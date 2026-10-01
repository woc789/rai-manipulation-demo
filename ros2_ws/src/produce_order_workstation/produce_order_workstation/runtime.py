"""ROS 2 adapter and the sequential order workflow."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from rai.communication.ros2 import wait_for_ros2_services, wait_for_ros2_topics
from rai.communication.ros2.connectors import ROS2Connector
from rai.communication.ros2.ros_async import get_future_result
from rai.types import Header as RaiHeader
from rai.types import Point as RaiPoint
from rai.types import Pose as RaiPose
from rai.types import PoseStamped as RaiPoseStamped
from rai.types import Quaternion as RaiQuaternion
from rai_interfaces.srv import ManipulatorMoveTo
from rai_sim.o3de.o3de_bridge import O3DExROS2Bridge
from rai_sim.simulation_bridge import Entity, SceneConfig
from std_msgs.msg import String
from tf2_ros import TransformException

from .order import (
    Order,
    OrderCancelled,
    OrderError,
    choose_source,
    distance_xy,
    parse_centroids,
    plan_placements,
    verify_placement,
)


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
        timeout = self.config["startup_timeout_s"]
        services = ["/manipulator_move_to"]
        if with_scene:
            services += ["/spawn_entity", "/delete_entity"]
        else:
            camera = self.config["camera"]
            wait_for_ros2_topics(
                self.connector,
                [camera["color"], camera["depth"], camera["info"]],
                timeout=timeout,
            )
            services += ["/detection", "/segmentation"]
        wait_for_ros2_services(self.connector, services, timeout=timeout)
        if with_scene:
            deadline = time.monotonic() + timeout
            while True:
                self.check_cancel()
                try:
                    self.connector.get_transform("world", "odom", timeout_sec=2.0)
                    return
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

    def build_scene(self, path: Path, order: Order) -> SceneConfig:
        """Combine fixed receiving bins with exactly the ordered products."""
        scene = SceneConfig.load_base_config(path)
        positions = self.config["stock"]["positions"]
        if order.total_items > len(positions):
            raise OrderError("Order exceeds the number of source positions")
        counts: dict[str, int] = {}
        item_index = 0
        for line in order.lines:
            for _ in range(line.quantity):
                counts[line.sku] = counts.get(line.sku, 0) + 1
                x, y = positions[item_index]
                rotation = self.config["stock"]["spawn_rotations"][line.sku]
                scene.entities.append(
                    Entity(
                        name=f"order_{line.sku}_{counts[line.sku]}",
                        prefab_name=line.sku,
                        pose=RaiPoseStamped(
                            header=RaiHeader(frame_id="odom"),
                            pose=RaiPose(
                                position=RaiPoint(x=x, y=y, z=0.05),
                                orientation=RaiQuaternion(**rotation),
                            ),
                        ),
                    )
                )
                item_index += 1
        return scene

    def prepare_scene(self, path: Path, order: Order) -> None:
        """Populate the benchmark with visible bins and ordered stock."""
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
        self._scene_bridge.setup_scene(self.build_scene(path, order))
        time.sleep(0.5)
        for zone in self.config["zones"].values():
            for index, slot in enumerate(zone["slots"]):
                self.slot_bin_position(zone, index, slot)

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

    def locate_source(self, sku: str, zone: dict):
        """Use simulator entity identity for a prepared scene; otherwise use vision."""
        if self._scene_bridge is None:
            return choose_source(self.detect(sku), self.config, zone), None
        candidates = []
        for entity in self._scene_bridge.spawned_entities:
            if entity.prefab_name == sku:
                position = self._scene_bridge.get_object_pose(entity).pose.position
                candidates.append((entity, (position.x, position.y, position.z)))
        source = choose_source([point for _, point in candidates], self.config, zone)
        entity = next(entity for entity, point in candidates if point == source)
        return source, entity

    def observe_after_place(self, sku: str, entity):
        if entity is None:
            return self.detect(sku)
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

    def run(self, order: Order) -> dict:
        zone = self.config["zones"][order.zone]
        placements = plan_placements(order, self.config)
        completed = 0
        item_cursor = 0
        failure = None
        self.event(order.order_id, "ORDER_STARTED", total=order.total_items)
        try:
            for line in order.lines:
                for index in range(line.quantity):
                    self.check_cancel()
                    placement = placements[item_cursor]
                    bin_index = placement.bin_index
                    slot = tuple(zone["slots"][bin_index])
                    target = (slot[0], slot[1] + placement.offset_y)
                    slot_id = f"{order.zone}{bin_index + 1}"
                    if self._scene_bridge is not None:
                        bin_position = self.slot_bin_position(zone, bin_index, slot)
                    else:
                        bin_position = slot
                    self.event(
                        order.order_id,
                        "DETECTING",
                        sku=line.sku,
                        item=index + 1,
                        slot_id=slot_id,
                        target=target,
                    )
                    source, entity = self.locate_source(line.sku, zone)
                    if distance_xy(source, target) <= 0.12:
                        raise OrderError("Source is too close to destination slot")
                    self.check_cancel()
                    self.event(
                        order.order_id,
                        "GRASPING",
                        sku=line.sku,
                        source=source,
                        position_source="scene_tf" if entity else "perception",
                    )
                    self.move(source, grip="grab")
                    self.event(
                        order.order_id,
                        "PLACING",
                        sku=line.sku,
                        slot_id=slot_id,
                        target=target,
                        bin_position=bin_position,
                    )
                    self.move(
                        (target[0], target[1], source[2]),
                        grip="release",
                        target_z=self.config["motion"]["bin_release_z_m"],
                    )
                    self.event(order.order_id, "RETRACTING", sku=line.sku, slot_id=slot_id)
                    self.move(
                        (target[0], target[1], source[2]),
                        grip="release",
                        target_z=self.config["motion"]["bin_clearance_z_m"],
                    )
                    time.sleep(self.config["motion"]["settle_time_s"])
                    if self._scene_bridge is not None:
                        bin_position = self.slot_bin_position(zone, bin_index, slot)
                    self.event(
                        order.order_id,
                        "VERIFYING",
                        sku=line.sku,
                        slot_id=slot_id,
                        bin_position=bin_position,
                    )
                    placed = verify_placement(
                        self.observe_after_place(line.sku, entity),
                        source,
                        bin_position,
                        zone["tolerance_m"],
                        self.config["workspace"]["source_exclusion_radius_m"],
                    )
                    if not placed:
                        raise OrderError(
                            f"{line.sku} was not observed at destination after release"
                        )
                    completed += 1
                    item_cursor += 1
                    self.event(
                        order.order_id,
                        "ITEM_COMPLETED",
                        sku=line.sku,
                        slot_id=slot_id,
                        completed=completed,
                        total=order.total_items,
                    )
        except OrderCancelled as exc:
            failure = str(exc)
            self.event(order.order_id, "ORDER_CANCELLED", reason=failure)
            status = "CANCELLED"
        except (OrderError, TimeoutError, ValueError) as exc:
            failure = str(exc)
            self.event(order.order_id, "ORDER_FAILED", reason=failure)
            status = "FAILED"
        else:
            self.event(order.order_id, "ORDER_COMPLETED", completed=completed)
            status = "COMPLETED"
        return {
            "order_id": order.order_id,
            "status": status,
            "completed": completed,
            "requested": order.total_items,
            "error": failure,
            "events": self.events,
        }
