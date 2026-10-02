"""ROS 2 workcell tools exposed to the goal-driven produce agent."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from rai.communication.ros2 import wait_for_ros2_services, wait_for_ros2_topics
from rai.communication.ros2.connectors import ROS2Connector
from rai.communication.ros2.ros_async import get_future_result
from rai_interfaces.srv import ManipulatorMoveTo
from rai_sim.o3de.o3de_bridge import O3DExROS2Bridge
from rai_sim.simulation_bridge import SceneConfig
from std_msgs.msg import String
from tf2_ros import TransformException

from .order import (
    Order,
    OrderCancelled,
    OrderError,
    PerceptionMismatch,
    choose_source,
    distance_xy,
    parse_centroids,
    plan_placements,
    verify_bin_placement,
)
from .agent import ProduceAgent


class Workstation:
    def __init__(
        self,
        connector: ROS2Connector,
        config: dict,
        *,
        event_sink: Callable[[dict], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ):
        self.connector = connector
        self.config = config
        self.publisher = connector.node.create_publisher(
            String, "/produce_orders/events", 10
        )
        self.events: list[dict] = []
        self._position_tool = None
        self._scene_bridge = None
        self._scene_prepared = False
        self._placed_entities: list[tuple] = []
        self._completed_by_sku: dict[str, int] = {}
        self._visual_candidates: dict[str, tuple[str, tuple[float, float, float]]] = {}
        self._perception_generation = 0
        self._rejected_visual_positions: dict[str, list[tuple[float, float, float]]] = {}
        self.motion_attempted = False
        self.event_sink = event_sink
        self.cancel_requested = cancel_requested or (lambda: False)

    def event(self, order_id: str, stage: str, **details) -> None:
        entry = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
            "stage": stage,
            **details,
        }
        self.events.append(entry)
        encoded = json.dumps(entry, ensure_ascii=False)
        self.publisher.publish(String(data=encoded))
        print(encoded, flush=True)
        if self.event_sink is not None:
            self.event_sink(entry)

    def check_cancel(self) -> None:
        if self.cancel_requested():
            raise OrderCancelled("Cancellation accepted before the next motion")

    def wait_until_ready(self, with_scene: bool = False) -> None:
        self._scene_prepared = False
        timeout = self.config["startup_timeout_s"]
        services = ["/manipulator_move_to", "/detection", "/segmentation"]
        if with_scene:
            services += ["/spawn_entity", "/delete_entity"]
        camera = self.config["camera"]
        wait_for_ros2_topics(
            self.connector,
            [camera["color"], camera["depth"], camera["info"]],
            timeout=timeout,
        )
        wait_for_ros2_services(self.connector, services, timeout=timeout)
        if with_scene:
            deadline = time.monotonic() + timeout
            while True:
                self.check_cancel()
                try:
                    self.connector.get_transform("world", "odom", timeout_sec=2.0)
                    break
                except TransformException as exc:
                    if time.monotonic() >= deadline:
                        raise OrderError("Simulator TF world/odom is not ready") from exc

        from rai_perception.tools import GetObjectPositionsTool
        from rai_perception.tools.gripping_points_tools import (
            GRIPPING_POINTS_TOOL_PARAM_PREFIX,
        )

        for name, value in {
            "camera_topic": camera["color"],
            "depth_topic": camera["depth"],
            "camera_info_topic": camera["info"],
            "target_frame": camera["target_frame"],
            "source_frame": camera["source_frame"],
            "timeout_sec": self.config["perception_timeout_s"],
        }.items():
            parameter_name = f"{GRIPPING_POINTS_TOOL_PARAM_PREFIX}.{name}"
            if not self.connector.node.has_parameter(parameter_name):
                self.connector.node.declare_parameter(parameter_name, value)
        if self._position_tool is None:
            self._position_tool = GetObjectPositionsTool(connector=self.connector)

    def build_scene(self, path: Path) -> SceneConfig:
        """Load receiving bins and an order-independent mixed table inventory."""
        return SceneConfig.load_base_config(path)

    def prepare_scene(self, path: Path, order: Order | None = None) -> None:
        """Populate the benchmark with visible bins and mixed, fixed stock."""
        if self._scene_bridge is None:
            self._scene_bridge = O3DExROS2Bridge(self.connector)
        # The Action server outlives O3DE. After a simulator restart its bridge
        # still remembers entities from the previous world, which no longer
        # exist. Remove only those confirmed missing by the delete service;
        # propagate every other cleanup error instead of spawning duplicates.
        while self._scene_bridge.spawned_entities:
            stale = self._scene_bridge.spawned_entities[0]
            try:
                self._scene_bridge.clear_scene()
            except RuntimeError as exc:
                if "Could not find entity with given name" not in str(exc):
                    raise
                self._scene_bridge.spawned_entities.remove(stale)
        self._scene_bridge.setup_scene(self.build_scene(path))
        time.sleep(self.config["stock"]["settle_time_s"])
        for zone in self.config["zones"].values():
            for index, slot in enumerate(zone["slots"]):
                self.slot_bin_position(zone, index, slot)
        self._scene_prepared = True
        self._placed_entities = []
        self._completed_by_sku = {}
        self._visual_candidates = {}
        self._perception_generation = 0
        self._rejected_visual_positions = {}
        self.motion_attempted = False
        zone = self.config["zones"]["A"]
        inventory = {sku: 0 for sku in self.config["aliases"]}
        for entity, _ in self._source_entities(zone).values():
            inventory[entity.prefab_name] += 1
        for sku, expected in self.config["stock"]["initial_counts"].items():
            if inventory[sku] < expected:
                raise OrderError(
                    f"Prepared scene has only {inventory[sku]}/{expected} reachable {sku}"
                )
        if order is not None:
            self.begin_order(order)

    def begin_order(self, order: Order) -> None:
        """Lock the current preloaded inventory to one new order."""
        if not self._scene_prepared:
            raise OrderError("Preloaded scene is not ready")
        self._placed_entities = []
        self._completed_by_sku = {line.sku: 0 for line in order.lines}
        self._visual_candidates = {}
        self._perception_generation = 0
        self._rejected_visual_positions = {}
        self.motion_attempted = False

    def slot_bin_position(self, zone: dict, index: int, slot: tuple) -> tuple:
        """Confirm that a visible receiving bin actually occupies this slot."""
        if self._scene_bridge is None:
            raise OrderError("Prepared scene is required to verify receiving bins")
        names = zone.get("slot_entities", [])
        if len(names) != len(zone["slots"]):
            raise OrderError("Each configured slot needs one receiving-bin entity")
        name = names[index]
        entity = next(
            (item for item in self._scene_bridge.spawned_entities if item.name == name),
            None,
        )
        if entity is None or entity.prefab_name != "toy_box":
            raise OrderError(f"Receiving bin {name} is missing from the scene")
        pose = self._scene_bridge.get_object_pose(entity).pose.position
        position = (pose.x, pose.y)
        if distance_xy(position, slot) > zone["tolerance_m"]:
            raise OrderError(f"Receiving bin {name} is not at its configured slot")
        return position

    def detect(self, sku: str) -> list[tuple[float, float, float]]:
        if self._position_tool is None:
            raise RuntimeError("wait_until_ready must be called first")
        for alias in self.config["aliases"][sku]:
            result = self._position_tool.invoke({"object_name": alias})
            points = parse_centroids(result)
            if points:
                return points
        return []

    def _source_entities(self, zone: dict) -> dict[str, tuple]:
        if not self._scene_prepared or self._scene_bridge is None:
            raise OrderError("Agent requires a prepared named-entity scene")
        result = {}
        already_placed = {entity.name for _, entity, _, _ in self._placed_entities}
        for entity in self._scene_bridge.spawned_entities:
            if entity.prefab_name not in self.config["aliases"]:
                continue
            if entity.name in already_placed:
                continue
            point = self._scene_bridge.get_object_pose(entity).pose.position
            xyz = (point.x, point.y, point.z)
            try:
                choose_source([xyz], self.config, zone)
            except OrderError:
                continue
            result[entity.name] = (entity, xyz)
        return result

    def get_object_positions(self, order: Order, sku: str) -> dict:
        """Agent-facing RGB-D perception; never reveal simulator entity labels."""
        self.check_cancel()
        requested = {line.sku: line.quantity for line in order.lines}
        if sku not in requested:
            raise OrderError(f"SKU {sku!r} is not requested by this order")
        zone = self.config["zones"][order.zone]
        # Every camera request invalidates all previous visual IDs, including
        # IDs for a different SKU. Never let an old ID silently refer to a
        # different candidate after the table changes.
        self._visual_candidates.clear()
        self._perception_generation += 1
        raw_points = self.detect(sku)
        points = []
        for point in sorted(raw_points, key=lambda value: (value[1], value[0])):
            try:
                choose_source([point], self.config, zone)
            except OrderError:
                continue
            if any(
                distance_xy(point, rejected) < self.config["perception"]["association_radius_m"]
                for rejected in self._rejected_visual_positions.get(sku, [])
            ):
                continue
            if any(distance_xy(point, existing) < 0.035 for existing in points):
                continue
            points.append(point)
        detections = []
        for index, point in enumerate(points, start=1):
            detection_id = f"visual_{sku}_{self._perception_generation}_{index}"
            self._visual_candidates[detection_id] = (sku, point)
            detections.append(
                {
                    "detection_id": detection_id,
                    "position_m": [round(value, 4) for value in point],
                }
            )
        result = {
            "sku": sku,
            "frame": self.config["camera"]["target_frame"],
            "source": "rgbd_detection_segmentation_depth_tf",
            "classification_verified": False,
            "detections": detections,
            "remaining": requested[sku] - self._completed_by_sku.get(sku, 0),
        }
        self.event(order.order_id, "PERCEPTION_RESULT", **result)
        return result

    def observe_after_place(self, sku: str, entity):
        position = self._scene_bridge.get_object_pose(entity).pose.position
        return [(position.x, position.y, position.z)]

    def move(
        self,
        point: tuple[float, float, float],
        *,
        grip: str,
        target_z: float | None = None,
    ) -> None:
        """Call the existing typed ROS service; stop on timeout or failure."""
        motion = self.config["motion"]
        request = ManipulatorMoveTo.Request()
        request.target_pose = PoseStamped()
        request.target_pose.header.frame_id = self.config["camera"]["target_frame"]
        extra_z = motion["grasp_z_offset_m"]
        if grip == "release":
            extra_z += motion["place_extra_height_m"]
        if target_z is None:
            target_z = point[2] + extra_z
        request.target_pose.pose = Pose(
            position=Point(
                x=point[0],
                y=point[1],
                z=max(target_z, motion["minimum_z_m"]),
            ),
            orientation=Quaternion(
                x=0.9238795325112867,
                y=-0.3826834323650898,
                z=0.0,
                w=0.0,
            ),
        )
        request.initial_gripper_state = grip == "grab"
        request.final_gripper_state = grip == "release"
        client = self.connector.node.create_client(
            ManipulatorMoveTo, "/manipulator_move_to"
        )
        if not client.wait_for_service(timeout_sec=5.0):
            raise OrderError("Manipulator service is unavailable")
        result = get_future_result(
            client.call_async(request), timeout_sec=motion["service_timeout_s"]
        )
        if result is None:
            raise OrderError(
                "Manipulator service timed out; physical state is unknown, stopping order"
            )
        if not result.success:
            raise OrderError(f"Manipulator {grip} motion failed")

    def pick_and_place(self, order: Order, detection_id: str) -> dict:
        """Re-localize a visual candidate, then execute validated ROS motion."""
        self.check_cancel()
        zone = self.config["zones"][order.zone]
        if detection_id not in self._visual_candidates:
            raise PerceptionMismatch(f"Unknown or stale visual detection {detection_id!r}; perceive again")
        sku, observed_source = self._visual_candidates[detection_id]
        requested = {line.sku: line.quantity for line in order.lines}
        done = self._completed_by_sku.get(sku, 0)
        if sku not in requested or done >= requested[sku]:
            raise PerceptionMismatch(f"Visual detection {detection_id!r} is not needed; choose a requested SKU")
        refreshed = self.detect(sku)
        if not refreshed:
            self._visual_candidates.clear()
            raise PerceptionMismatch("Visual target disappeared; perceive the table again")
        source = min(refreshed, key=lambda point: distance_xy(point, observed_source))
        if distance_xy(source, observed_source) > self.config["perception"]["association_radius_m"]:
            self._visual_candidates.clear()
            raise PerceptionMismatch("Visual target moved or detection changed; perceive again")
        choose_source([source], self.config, zone)
        # Simulation-only safety oracle. Its labels/positions are never exposed
        # to the Agent and never used as the gripper target; only the RGB-D
        # estimate above drives the grasp. It prevents a known false positive
        # (for example a tomato classified as an apple) from causing motion.
        truth = self._source_entities(zone)
        if not truth:
            raise PerceptionMismatch("No physical stock matches the visual candidate")
        entity, actual = min(
            truth.values(), key=lambda item: distance_xy(item[1], source)
        )
        if (
            distance_xy(actual, source) > self.config["perception"]["association_radius_m"]
            or entity.prefab_name != sku
        ):
            self._rejected_visual_positions.setdefault(sku, []).append(source)
            self._visual_candidates.pop(detection_id, None)
            raise PerceptionMismatch("Visual class/position failed safety check; choose another detection")
        placements = [
            item for item in plan_placements(order, self.config) if item.sku == sku
        ]
        placement = placements[done]
        bin_index = placement.bin_index
        slot = tuple(zone["slots"][bin_index])
        target = (slot[0] + placement.offset_x, slot[1] + placement.offset_y)
        slot_id = f"{order.zone}{bin_index + 1}"
        bin_position = self.slot_bin_position(zone, bin_index, slot)
        if distance_xy(source, target) <= 0.12:
            raise OrderError("Source is too close to destination slot")
        self.event(
            order.order_id, "GRASPING", sku=sku, detection_id=detection_id,
            source=source, position_source="rgbd_perception",
        )
        self.motion_attempted = True
        self.move(source, grip="grab")
        self.event(
            order.order_id, "PLACING", sku=sku, detection_id=detection_id,
            slot_id=slot_id, target=target, bin_position=bin_position,
        )
        self.move(
            (target[0], target[1], source[2]),
            grip="release", target_z=self.config["motion"]["bin_release_z_m"],
        )
        self.event(order.order_id, "RETRACTING", sku=sku, slot_id=slot_id)
        self.move(
            (target[0], target[1], source[2]),
            grip="release", target_z=self.config["motion"]["bin_clearance_z_m"],
        )
        time.sleep(self.config["motion"]["settle_time_s"])
        bin_position = self.slot_bin_position(zone, bin_index, slot)
        self.event(order.order_id, "VERIFYING", sku=sku, slot_id=slot_id)
        if not verify_bin_placement(
            self.observe_after_place(sku, entity), source, bin_position,
            tuple(zone["bin_half_extents_m"]),
            self.config["workspace"]["source_exclusion_radius_m"],
        ):
            raise OrderError(f"{sku} was not observed inside bin {slot_id}")
        self._completed_by_sku[sku] = done + 1
        self._placed_entities.append((sku, entity, bin_index, source))
        self._visual_candidates.clear()
        completed = sum(self._completed_by_sku.values())
        self.event(
            order.order_id, "ITEM_COMPLETED", sku=sku, detection_id=detection_id,
            slot_id=slot_id, completed=completed, total=order.total_items,
        )
        return {
            "placed": detection_id, "sku": sku, "bin": slot_id,
            "completed": completed,
            "next": "Call get_object_positions again before another pick",
        }

    def finish_order(self, order: Order) -> dict:
        """The Agent can request completion, but cannot declare success itself."""
        self.check_cancel()
        for line in order.lines:
            if self._completed_by_sku.get(line.sku, 0) != line.quantity:
                raise OrderError(f"Order is incomplete: {line.sku}")
        zone = self.config["zones"][order.zone]
        self.event(
            order.order_id, "FINAL_AUDIT",
            completed=len(self._placed_entities), total=order.total_items,
        )
        for sku, entity, bin_index, source in self._placed_entities:
            slot = tuple(zone["slots"][bin_index])
            bin_position = self.slot_bin_position(zone, bin_index, slot)
            if not verify_bin_placement(
                self.observe_after_place(sku, entity), source, bin_position,
                tuple(zone["bin_half_extents_m"]),
                self.config["workspace"]["source_exclusion_radius_m"],
            ):
                raise OrderError(
                    f"Final audit found {sku} outside bin {order.zone}{bin_index + 1}"
                )
        return {"status": "verified", "completed": len(self._placed_entities)}

    def run(self, order: Order, *, model=None) -> dict:
        """Ask the model to solve a goal with tools, preserving a failure report."""
        failure = None
        self.event(order.order_id, "ORDER_STARTED", total=order.total_items)
        try:
            if not self._scene_prepared:
                raise OrderError("Agent execution requires prepare_scene=true")
            ProduceAgent(self, order, model=model).run()
        except OrderCancelled as exc:
            failure = str(exc)
            self.event(order.order_id, "ORDER_CANCELLED", reason=failure)
            status = "CANCELLED"
        except Exception as exc:
            failure = str(exc)
            self.event(order.order_id, "ORDER_FAILED", reason=failure)
            status = "FAILED"
        else:
            self.event(order.order_id, "ORDER_COMPLETED", completed=len(self._placed_entities))
            status = "COMPLETED"
        return {
            "order_id": order.order_id, "status": status,
            "completed": len(self._placed_entities), "requested": order.total_items,
            "error": failure, "events": self.events,
        }
