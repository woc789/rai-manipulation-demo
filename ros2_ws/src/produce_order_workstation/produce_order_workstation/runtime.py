"""ROS 2 workcell tools exposed to the goal-driven produce agent."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from action_msgs.msg import GoalStatus
from rai.communication.ros2 import wait_for_ros2_services, wait_for_ros2_topics
from rai.communication.ros2.connectors import ROS2Connector
from rai.communication.ros2.ros_async import get_future_result
from rclpy.action import ActionClient
from rai_sim.o3de.o3de_bridge import O3DExROS2Bridge
from rai_sim.simulation_bridge import SceneConfig
from std_msgs.msg import String
from std_srvs.srv import Trigger
from tf2_ros import TransformException
from produce_order_interfaces.action import ExecuteMotion

from .order import (
    Order,
    OrderCancelled,
    OrderPaused,
    OrderError,
    PerceptionMismatch,
    MotionPlanRejected,
    calibrated_grasp_point,
    choose_source,
    distance_xy,
    fulfillment_mode,
    parse_centroids,
    plan_placements,
    verify_bin_placement,
)
from .planning_scene import PlanningSceneSync
from .database import WorkcellDatabase
from .inventory_repository import InventoryRepository
from .resource_repository import ResourceRepository
from .execution_gate import serialized_skill
from .stock_layout import validate_stock_layout


class Workstation:
    def __init__(
        self,
        connector: ROS2Connector,
        config: dict,
        *,
        event_sink: Callable[[dict], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        store=None,
    ):
        self.connector = connector
        self.config = config
        self.publisher = connector.node.create_publisher(
            String, "/produce_orders/events", 10
        )
        self.events: list[dict] = []
        self._position_tool = None
        self._scene_bridge = None
        self._planning_scene = None
        self._scene_prepared = False
        self.store = store if store is not None else WorkcellDatabase(":memory:")
        self.resources = ResourceRepository(self.store, config)
        self._allocated_bin_index: int | None = None
        self._bin_templates = {}
        self._visual_candidates: dict[str, tuple[str, tuple[float, float, float]]] = {}
        self._inventory_candidates: dict[str, str] = {}
        perception = config.get("perception", {})
        self.inventory = InventoryRepository(
            self.store,
            association_radius=perception.get("association_radius_m", 0.035),
            ttl=perception.get("inventory_ttl_s", 60.0),
            record_observations=config.get("persistence", {}).get(
                "record_observations", False
            ),
            observation_limit=config.get("persistence", {}).get(
                "max_observations", 1000
            ),
        )
        self._perception_generation = 0
        self._rejected_visual_positions: dict[
            str, list[tuple[float, float, float]]
        ] = {}
        self._candidate_rejections = {}
        self._visual_observed_at = 0.0
        self.physical_state_uncertain = False
        self._needs_observation_motion = False
        self.motion_attempted = False
        self._motion_client = None
        self._active_order_id: str | None = None
        self.require_order_reservation = False
        self.yield_requested = lambda: False
        self.event_sink = event_sink
        self.cancel_requested = cancel_requested or (lambda: False)
        self._mcp_server = None
        self._agent_dispatcher = None
        self.execution_runner = None
        self.durable_event_sink = None

    @property
    def _completed_by_sku(self):
        return (
            self.resources.progress(self._active_order_id)
            if self._active_order_id
            else {}
        )

    @property
    def _placed_entities(self):
        records = (
            self.resources.placements(self._active_order_id)
            if self._active_order_id
            else []
        )
        tracked = (
            {entity.name: entity for entity in self._scene_bridge.spawned_entities}
            if self._scene_bridge
            else {}
        )
        return [
            (sku, tracked.get(entity.name, entity), index, source)
            for sku, entity, index, source in records
        ]

    @property
    def _used_placement_targets(self):
        return (
            self.resources.targets(self._active_order_id)
            if self._active_order_id
            else []
        )

    @property
    def _delivered_entity_names(self):
        return self.resources.delivered_entity_names()

    def event(self, order_id: str, stage: str, **details) -> None:
        entry = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "order_id": order_id,
            "stage": stage,
            **details,
        }
        self.events.append(entry)
        if self.durable_event_sink is not None:
            self.durable_event_sink(entry)
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
                        raise OrderError(
                            "Simulator TF world/odom is not ready"
                        ) from exc

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
            pipeline = (
                self._position_tool.gripping_points_tool.point_cloud_from_segmentation
            )
            pipeline.check_cancel = self.check_cancel
            pipeline.config.service_timeout = self.config.get("perception", {}).get(
                "service_timeout_s", 20.0
            )
            pipeline.config.detection_roi = self.config.get("perception", {}).get(
                "detection_roi"
            )
            pipeline.config.include_full_frame = self.config.get("perception", {}).get(
                "include_full_frame", False
            )
            pipeline.config.max_rgb_depth_skew_s = self.config.get(
                "perception", {}
            ).get("max_rgb_depth_skew_s", 0.1)
        if self._motion_client is None:
            self._motion_client = ActionClient(
                self.connector.node, ExecuteMotion, "/produce_orders/execute_motion"
            )
        if not self._motion_client.wait_for_server(timeout_sec=timeout):
            raise OrderError("Manipulator Action is not ready")

    def build_scene(self, path: Path) -> SceneConfig:
        """Load receiving bins and an order-independent mixed table inventory."""
        return SceneConfig.load_base_config(path)

    def prepare_scene(self, path: Path, order: Order | None = None) -> None:
        """Populate the benchmark with visible bins and mixed, fixed stock."""
        self._scene_prepared = False
        scene = self.build_scene(path)
        # Fail before deleting the previous scene if the new layout is invalid.
        validate_stock_layout(scene, self.config)
        if self._scene_bridge is None:
            self._scene_bridge = O3DExROS2Bridge(self.connector)
        self._scene_bridge.service_timeout_sec = self.config.get(
            "scene_service_timeout_s", 20.0
        )
        # The Action server outlives O3DE. After a simulator restart its bridge
        # still remembers entities from the previous world, which no longer
        # exist. Remove only those confirmed missing by the delete service;
        # propagate every other cleanup error instead of spawning duplicates.
        while self._scene_bridge.spawned_entities:
            stale = self._scene_bridge.spawned_entities[0]
            try:
                self._scene_bridge._despawn_entity(stale)
            except RuntimeError as exc:
                if "Could not find entity with given name" not in str(exc):
                    raise
                self._scene_bridge.spawned_entities.remove(stale)
        bin_names = {
            name
            for zone in self.config["zones"].values()
            for name in zone["slot_entities"]
        }
        self._bin_templates = {
            item.name: item for item in scene.entities if item.name in bin_names
        }
        if set(self._bin_templates) != bin_names:
            raise OrderError("Receiving-bin templates are incomplete")
        barrier_config = self.config["planning_scene"]["barrier"]
        barrier_names = (
            barrier_config.get("entities") or barrier_config["cube_entities"]
        )
        barrier_entities = [
            item for item in scene.entities if item.name in barrier_names
        ]
        if [item.name for item in barrier_entities] != barrier_names or any(
            item.prefab_name != barrier_config.get("prefab_name", "yellow_cube")
            for item in barrier_entities
        ):
            raise OrderError("Physical route marker is incomplete or out of order")
        self._scene_bridge.setup_scene(
            SceneConfig(
                entities=[
                    item
                    for item in scene.entities
                    if item.name not in barrier_names
                    and (
                        fulfillment_mode(self.config) != "fulfillment"
                        or item.name not in bin_names
                    )
                ]
            )
        )
        time.sleep(self.config["stock"]["settle_time_s"])
        # These are dynamic rigid bodies. Spawn them one at a time so the stack
        # settles instead of exploding apart when O3DE resolves simultaneous
        # collider penetration. The bridge records each entity for later cleanup.
        for item in barrier_entities:
            self._scene_bridge._spawn_entity(item)
            time.sleep(self.config["planning_scene"]["barrier"]["spawn_settle_time_s"])
        scene_objects = self.synchronize_scene()
        self._scene_prepared = True
        self.inventory.reset()
        self._active_order_id = None
        self._allocated_bin_index = None
        self._visual_candidates = {}
        self._perception_generation = 0
        self._rejected_visual_positions = {}
        self._candidate_rejections = {}
        self._visual_observed_at = 0.0
        self.physical_state_uncertain = False
        self.motion_attempted = False
        self._active_order_id = None
        self._needs_observation_motion = True
        zone = self.config["zones"]["A"]
        inventory = {sku: 0 for sku in self.config["aliases"]}
        for entity, _ in self._source_entities(zone).values():
            inventory[entity.prefab_name] += 1
        for sku, expected in self.config["stock"]["initial_counts"].items():
            if inventory[sku] < expected:
                self._scene_prepared = False
                raise OrderError(
                    f"Prepared scene has only {inventory[sku]}/{expected} reachable {sku}"
                )
        self.event("", "PLANNING_SCENE_READY", objects=scene_objects)
        if order is not None:
            indices = (
                (0,)
                if fulfillment_mode(self.config) == "fulfillment"
                else tuple(zone["sku_bins"][line.sku] for line in order.lines)
            )
            self.begin_order(order, bin_index=indices[0])
            self.ensure_order_bins(order, indices)

    def synchronize_scene(self) -> list[str]:
        """Publish only physically present bins, plus the measured obstacle."""
        bin_positions = {}
        present = {item.name for item in self._scene_bridge.spawned_entities}
        for zone in self.config["zones"].values():
            for index, slot in enumerate(zone["slots"]):
                name = zone["slot_entities"][index]
                if (
                    name not in present
                    and fulfillment_mode(self.config) == "fulfillment"
                ):
                    continue
                xy = self.slot_bin_position(zone, index, slot)
                entity = next(
                    item
                    for item in self._scene_bridge.spawned_entities
                    if item.name == name
                )
                pose = self._scene_bridge.get_object_pose(entity).pose.position
                bin_positions[name] = (xy[0], xy[1], pose.z)
        barrier_poses = []
        barrier_config = self.config["planning_scene"]["barrier"]
        for name in barrier_config.get("entities") or barrier_config["cube_entities"]:
            entity = next(
                (
                    item
                    for item in self._scene_bridge.spawned_entities
                    if item.name == name
                ),
                None,
            )
            if entity is None or entity.prefab_name != barrier_config.get(
                "prefab_name", "yellow_cube"
            ):
                raise OrderError(f"Physical route marker cube {name} is missing")
            barrier_poses.append(self._scene_bridge.get_object_pose(entity).pose)
        if self._planning_scene is None:
            self._planning_scene = PlanningSceneSync(
                self.connector.node,
                timeout_s=self.config["planning_scene"]["service_timeout_s"],
            )
        return self._planning_scene.synchronize(
            self.config, bin_positions, barrier_poses
        )

    def ensure_order_bins(self, order: Order, indices: tuple[int, ...]) -> None:
        if fulfillment_mode(self.config) != "fulfillment":
            return
        zone = self.config["zones"][order.zone]
        existing = {e.name for e in self._scene_bridge.spawned_entities}
        for index in indices:
            name = zone["slot_entities"][index]
            if name in existing:
                raise OrderError(
                    f"Allocated empty bin slot {name} already contains a physical bin"
                )
            self._scene_bridge._spawn_entity(self._bin_templates[name])
        time.sleep(self.config["stock"]["settle_time_s"])
        objects = self.synchronize_scene()
        self.event(
            order.order_id,
            "ORDER_BIN_ARRIVED",
            bin_ids=[f"{order.zone}{i + 1}" for i in indices],
            objects=objects,
        )

    def remove_order_bins(self, order: Order, indices: tuple[int, ...]) -> None:
        """Remove only the assigned boxes, after goods pickup or a no-motion failure."""
        if fulfillment_mode(self.config) != "fulfillment":
            return
        zone = self.config["zones"][order.zone]
        for index in indices:
            name = zone["slot_entities"][index]
            entity = next(
                (e for e in self._scene_bridge.spawned_entities if e.name == name), None
            )
            if entity is not None:
                self._scene_bridge._despawn_entity(entity)
        self.synchronize_scene()

    def begin_order(self, order: Order, *, bin_index: int | None = None) -> None:
        """Lock the current preloaded inventory to one new order."""
        if not self._scene_prepared:
            raise OrderError("Preloaded scene is not ready")
        if self.resources.get(order.order_id) is None:
            self.resources.submit(order)
        elif self.resources.get(order.order_id).order != order:
            raise OrderError("Execution order differs from database demand")
        self._visual_candidates = {}
        self._inventory_candidates = {}
        self._perception_generation = 0
        self._rejected_visual_positions = {}
        self._candidate_rejections = {}
        self._visual_observed_at = 0.0
        self.physical_state_uncertain = False
        self.motion_attempted = False
        self._active_order_id = order.order_id
        self._allocated_bin_index = bin_index
        self.event(
            order.order_id,
            "ORDER_BIN_ASSIGNED",
            bin_id=(
                f"{order.zone}{bin_index + 1}"
                if bin_index is not None
                else "classification/default"
            ),
        )

    def collect_order(self, order: Order, placed_entities: list[tuple]) -> None:
        """Simulate human pickup, deleting only this order's verified goods.

        Preflight all remaining goods before deleting any. Successfully deleted
        entities disappear from bridge tracking, making partial cleanup retryable.
        Bin geometry and source-table stock are never deleted here.
        """
        if self._scene_bridge is None or not self._scene_prepared:
            raise OrderError("Prepared scene is required for pickup")
        tracked = {
            entity.name: entity for entity in self._scene_bridge.spawned_entities
        }
        remaining = []
        for sku, entity, bin_index, source in placed_entities:
            if entity.name not in tracked:
                continue  # A previous pickup attempt already deleted this entity.
            entity = tracked[entity.name]
            # Entity identity is bookkeeping only, never a grasp coordinate source.
            zone = self.config["zones"][order.zone]
            if entity.name not in self._delivered_entity_names:
                raise OrderError(
                    f"Pickup item {entity.name} is not a verified delivered item"
                )
            slot = tuple(zone["slots"][bin_index])
            center = self.slot_bin_position(zone, bin_index, slot)
            if not verify_bin_placement(
                self.observe_after_place(sku, entity),
                source,
                center,
                tuple(zone["bin_half_extents_m"]),
                self.config["workspace"]["source_exclusion_radius_m"],
            ):
                raise OrderError(
                    f"Pickup item {entity.name} moved outside its bin; inspect the scene"
                )
            remaining.append(entity)
        for entity in remaining:
            self._scene_bridge._despawn_entity(entity)
        self.remove_order_bins(
            order, tuple(sorted({entry[2] for entry in placed_entities}))
        )
        self.event(
            order.order_id,
            "PICKUP_CONFIRMED",
            removed_entities=[e.name for e in remaining],
        )

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
            self.check_cancel()
            try:
                result = self._position_tool.invoke({"object_name": alias})
            except TimeoutError as exc:
                raise OrderError(
                    f"Perception service unavailable: {exc}; stop instead of retrying aliases"
                ) from exc
            self.check_cancel()
            if str(result).startswith("Timeout:"):
                raise OrderError(
                    "Perception tool timed out; stop instead of treating it as missing stock"
                )
            points = parse_centroids(result)
            self._record_perception_pipeline(sku, alias, points)
            # An alias producing only recently rejected candidates is not a
            # usable result. Try the next configured alias, still bounded.
            if points and any(not self._recently_rejected(sku, p) for p in points):
                return points
        return []

    def _recently_rejected(self, sku, point):
        now = time.monotonic()
        ttl = self.config.get("perception", {}).get("rejection_cooldown_s", 20.0)
        radius = self.config.get("perception", {}).get("rejection_radius_m", 0.02)
        self._candidate_rejections = {
            key: value
            for key, value in self._candidate_rejections.items()
            if now - value["time"] < ttl
        }
        return any(
            record["sku"] == sku and distance_xy(point, record["position"]) < radius
            for record in self._candidate_rejections.values()
        )

    def _reject_visual_candidate(self, order, detection_id, sku, source, code, details):
        self._candidate_rejections[detection_id] = dict(
            sku=sku, position=source, time=time.monotonic(), code=code
        )
        item_id = self._inventory_candidates.get(detection_id)
        if item_id is not None:
            self.inventory.reject_candidate(item_id, order.order_id)
        self._visual_candidates.pop(detection_id, None)
        self.event(
            order.order_id,
            "CANDIDATE_REJECTED",
            detection_id=detection_id,
            sku=sku,
            reason_code=code,
            visual_position=list(source),
            **details,
        )

    def _choose_source(self, points, zone):
        # In fulfillment mode, absent box templates are not physical exclusion
        # zones. Never expose simulator object labels/coordinates as detections.
        if (
            fulfillment_mode(self.config) == "fulfillment"
            and self._scene_prepared
            and self._scene_bridge
        ):
            existing = {entity.name for entity in self._scene_bridge.spawned_entities}
            zone = dict(
                zone,
                slots=[
                    slot
                    for slot, name in zip(zone["slots"], zone["slot_entities"])
                    if name in existing
                ],
            )
        return choose_source(points, self.config, zone)

    def _record_perception_pipeline(self, sku, alias, points):
        pipeline = getattr(
            getattr(self._position_tool, "gripping_points_tool", None),
            "point_cloud_from_segmentation",
            None,
        )
        diagnostics = getattr(pipeline, "last_diagnostics", None)
        if not isinstance(diagnostics, dict) or not diagnostics:
            return
        details = dict(diagnostics)
        details["position_count"] = len(points)
        if not points and details.get("cloud_count", 0):
            details["empty_stage"] = "point_filter_or_estimator"
        if not points:
            details.update(self._save_perception_image(pipeline))
        self.event(
            self._active_order_id or "",
            "PERCEPTION_PIPELINE",
            sku=sku,
            alias=alias,
            **details,
        )

    def _save_perception_image(self, pipeline=None):
        """Save evidence only; a diagnostic write failure cannot change execution."""
        try:
            if pipeline is None:
                pipeline = self._position_tool.gripping_points_tool.point_cloud_from_segmentation
            image = getattr(pipeline, "last_image", None)
            if image is None:
                return {}
            import cv2
            from cv_bridge import CvBridge

            directory = (
                Path.home() / ".local/share/produce_order_workstation/perception"
            )
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"camera-{time.time_ns()}.jpg"
            if not cv2.imwrite(
                str(path), CvBridge().imgmsg_to_cv2(image, desired_encoding="bgr8")
            ):
                raise RuntimeError("Failed to save camera diagnostic image")
            return {"image_path": str(path)}
        except Exception as exc:
            return {"diagnostic_image_error": str(exc)}

    def _ensure_observation_posture(self):
        """Return an empty, verified arm to its captured startup posture.

        Existing reset_manipulator plans collision-checked joint motion. It does
        not reset inventory, boxes, orders or the simulator scene.
        """
        if not self._needs_observation_motion or not self.config["motion"].get(
            "return_to_observation", False
        ):
            return
        if self.physical_state_uncertain or self.inventory.has_active_execution():
            raise OrderError(
                "Cannot change observation posture with uncertain/active grasp"
            )
        self.check_cancel()
        client = self.connector.node.create_client(Trigger, "/reset_manipulator")
        try:
            if not client.wait_for_service(timeout_sec=3.0):
                raise OrderError("Observation posture service unavailable")
            self.event(
                self._active_order_id or "",
                "OBSERVATION_POSTURE",
                status="moving_empty_arm",
            )
            self.physical_state_uncertain = True
            self.motion_attempted = True
            response = get_future_result(
                client.call_async(Trigger.Request()),
                timeout_sec=self.config["motion"]["service_timeout_s"],
            )
            if response is None or not response.success:
                raise OrderError(
                    "Observation posture motion failed/timed out; inspect physical state"
                )
            self.physical_state_uncertain = False
            self._needs_observation_motion = False
            self._visual_observed_at = 0.0
            time.sleep(0.2)
            self.check_cancel()
            self.event(
                self._active_order_id or "", "OBSERVATION_POSTURE", status="ready"
            )
        finally:
            self.connector.node.destroy_client(client)

    def scan_inventory(self, skus, zone_name="A"):
        self._ensure_observation_posture()
        """Idle-only caller: populate ledger from RGB-D, never simulator truth."""
        zone = self.config["zones"][zone_name]
        for sku in skus:
            points = []
            for point in self.detect(sku):
                try:
                    self._choose_source([point], zone)
                except OrderError:
                    continue
                if not any(distance_xy(point, existing) < 0.035 for existing in points):
                    points.append(point)
            self.inventory.observe(sku, self.config["camera"]["target_frame"], points)
        return self.inventory.summary()

    def replenish_source(self, scene_path):
        """Explicit idle maintenance: replace only source stock, preserve order boxes."""
        if self._scene_bridge is None or not self._scene_prepared:
            raise OrderError("场景未准备，不能补货")
        scene_config = self.build_scene(Path(scene_path))
        validate_stock_layout(scene_config, self.config)
        for entity in list(self._scene_bridge.spawned_entities):
            if (
                entity.prefab_name in self.config["aliases"]
                and entity.name not in self._delivered_entity_names
            ):
                self._scene_bridge._despawn_entity(entity)
        # Delivered entities keep their names; new stock gets unique names.
        generation = self.inventory.epoch + 1
        for entity in scene_config.entities:
            if entity.prefab_name in self.config["aliases"]:
                entity.name = f"{entity.name}_refill_{generation}"
                self._scene_bridge._spawn_entity(entity)
        self.inventory.invalidate_source()
        time.sleep(self.config["motion"]["settle_time_s"])
        self.synchronize_scene()

    def _source_entities(self, zone: dict) -> dict[str, tuple]:
        if not self._scene_prepared or self._scene_bridge is None:
            raise OrderError("Agent requires a prepared named-entity scene")
        result = {}
        already_placed = self._delivered_entity_names | {
            entity.name for _, entity, _, _ in self._placed_entities
        }
        for entity in self._scene_bridge.spawned_entities:
            if entity.prefab_name not in self.config["aliases"]:
                continue
            if entity.name in already_placed:
                continue
            point = self._scene_bridge.get_object_pose(entity).pose.position
            xyz = (point.x, point.y, point.z)
            try:
                self._choose_source([xyz], zone)
            except OrderError:
                continue
            result[entity.name] = (entity, xyz)
        return result

    def _next_placement(self, order: Order, sku: str):
        placements = [
            item
            for item in plan_placements(
                order, self.config, bin_index=self._allocated_bin_index
            )
            if item.sku == sku
        ]
        done = self._completed_by_sku.get(sku, 0)
        if done >= len(placements):
            raise PerceptionMismatch(
                f"No remaining placement for {sku}; choose a requested SKU"
            )
        placement = placements[done]
        slot = self.config["zones"][order.zone]["slots"][placement.bin_index]
        return placement, (slot[0] + placement.offset_x, slot[1] + placement.offset_y)

    def _source_target_clearance(self):
        return self.config["workspace"].get("source_target_clearance_m", 0.12)

    def _placement_target(self, order, sku, source):
        placement, preferred = self._next_placement(order, sku)
        if fulfillment_mode(self.config) != "fulfillment":
            return placement, preferred
        center = self.config["zones"][order.zone]["slots"][placement.bin_index]
        options = [preferred] + [
            (center[0] + dx, center[1] + dy)
            for dx, dy in ((-0.03, -0.04), (-0.03, 0.04), (0.03, -0.04), (0.03, 0.04))
        ]
        for target in options:
            if distance_xy(source, target) > self._source_target_clearance() and all(
                distance_xy(target, used) >= 0.04 - 1e-6
                for used in self._used_placement_targets
            ):
                return placement, target
        raise PerceptionMismatch(
            "No unoccupied destination offset has safe source clearance; choose another candidate",
            code="DESTINATION_CONFLICT",
            reobserve=False,
        )

    @serialized_skill
    def get_object_positions(self, order: Order, sku: str) -> dict:
        """Agent-facing RGB-D perception; never reveal simulator entity labels."""
        self._ensure_observation_posture()
        self.check_cancel()
        requested = {line.sku: line.quantity for line in order.lines}
        if sku not in requested:
            raise OrderError(f"SKU {sku!r} is not requested by this order")
        zone = self.config["zones"][order.zone]
        # Every camera request invalidates all previous visual IDs, including
        # IDs for a different SKU. Never let an old ID silently refer to a
        # different candidate after the table changes.
        self._visual_candidates.clear()
        self._inventory_candidates.clear()
        self._perception_generation += 1
        raw_points = self.detect(sku)
        points = []
        filtered = {
            "workspace_or_bin": 0,
            "previous_safety_rejection": 0,
            "duplicate": 0,
            "target_clearance": 0,
            "reservation": 0,
            "class_ambiguity": 0,
        }
        for point in sorted(raw_points, key=lambda value: (value[1], value[0])):
            try:
                self._choose_source([point], zone)
            except OrderError:
                filtered["workspace_or_bin"] += 1
                continue
            if self._recently_rejected(sku, point):
                filtered["previous_safety_rejection"] += 1
                continue
            if any(distance_xy(point, existing) < 0.035 for existing in points):
                filtered["duplicate"] += 1
                continue
            points.append(point)
        detections = []
        inventory_ids = self.inventory.observe(
            sku, self.config["camera"]["target_frame"], points
        )
        _, target = self._next_placement(order, sku)
        excluded = set()
        for point, item_id in zip(points, inventory_ids):
            try:
                _, adjusted = self._placement_target(order, sku, point)
                if distance_xy(point, adjusted) <= self._source_target_clearance():
                    excluded.add(item_id)
            except PerceptionMismatch:
                excluded.add(item_id)
        if excluded:
            self.event(
                order.order_id,
                "CANDIDATE_CLEARANCE_REJECTED",
                sku=sku,
                target=list(target),
                minimum_distance_m=self._source_target_clearance(),
                excluded_item_ids=sorted(excluded),
            )
        if self.require_order_reservation:
            # A rejected pre-motion candidate can be replaced only with free
            # visual stock; never steal a different order's reservation.
            self.inventory.reserve_order(order, excluded_item_ids=excluded)
        for index, (point, item_id) in enumerate(zip(points, inventory_ids), start=1):
            if item_id in excluded:
                filtered["target_clearance"] += 1
                continue
            if self.inventory.conflicting_labels(item_id):
                filtered["class_ambiguity"] += 1
                continue
            if not self.inventory.selectable(item_id, order.order_id):
                filtered["reservation"] += 1
                continue
            # Free current-frame candidates are alternatives, not extra reserved
            # quantity. Selection exchanges a reservation atomically before motion.
            detection_id = f"visual_{sku}_{self._perception_generation}_{index}"
            self._visual_candidates[detection_id] = (sku, point)
            self._inventory_candidates[detection_id] = item_id
            detections.append(
                {
                    "detection_id": detection_id,
                    "position_m": [round(value, 4) for value in point],
                    "inventory_item_id": item_id,
                }
            )
        self._visual_observed_at = time.monotonic()
        result = {
            "sku": sku,
            "frame": self.config["camera"]["target_frame"],
            "source": "rgbd_detection_segmentation_depth_tf",
            "classification_verified": False,
            "detections": detections,
            "remaining": requested[sku] - self._completed_by_sku.get(sku, 0),
            "raw_position_count": len(raw_points),
            "filtered_counts": filtered,
        }
        if not detections and raw_points:
            result["empty_reason"] = (
                "Overlapping product labels need another view or operator review; no SKU was verified"
                if filtered["class_ambiguity"]
                else "All localized candidates were rejected by workspace, safety, or reservation rules"
            )
            result.update(self._save_perception_image())
        self.event(order.order_id, "PERCEPTION_RESULT", **result)
        return result

    def entity_position(self, entity) -> tuple[float, float, float]:
        position = self._scene_bridge.get_object_pose(entity).pose.position
        return (position.x, position.y, position.z)

    def observe_after_place(self, sku: str, entity):
        return [self.entity_position(entity)]

    @serialized_skill
    def move(
        self,
        point: tuple[float, float, float],
        *,
        step: str,
        target_z: float | None = None,
    ) -> None:
        """Send a bounded ROS Action goal and report motion-stage feedback."""
        gripper_states = {
            "grab": (True, False),
            "lift": (False, False),
            "place": (False, True),
            "retreat": (True, True),
        }
        if step not in gripper_states:
            raise ValueError(f"Unsupported motion step {step!r}")
        motion = self.config["motion"]
        goal = ExecuteMotion.Goal()
        goal.step = step
        goal.target_pose = PoseStamped()
        goal.target_pose.header.frame_id = self.config["camera"]["target_frame"]
        if target_z is None:
            target_z = {
                "grab": point[2] + motion["grasp_z_offset_m"],
                "lift": motion["post_grasp_lift_z_m"],
                "place": motion["bin_release_z_m"],
                "retreat": motion["bin_clearance_z_m"],
            }[step]
        goal.target_pose.pose = Pose(
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
        goal.initial_gripper_state, goal.final_gripper_state = gripper_states[step]
        if self._motion_client is None:
            self._motion_client = ActionClient(
                self.connector.node, ExecuteMotion, "/produce_orders/execute_motion"
            )
        if not self._motion_client.wait_for_server(timeout_sec=5.0):
            raise OrderError("Manipulator Action is unavailable")

        def on_feedback(message):
            self.event(
                self._active_order_id or "",
                "MOTION_STAGE",
                step=step,
                motion_stage=message.feedback.stage,
            )

        handle = get_future_result(
            self._motion_client.send_goal_async(goal, feedback_callback=on_feedback),
            timeout_sec=5.0,
        )
        if handle is None:
            raise OrderError(
                "Manipulator Action acceptance timed out; state is unknown"
            )
        if not handle.accepted:
            raise OrderError(f"Manipulator {step} goal was rejected")
        result_future = handle.get_result_async()
        deadline = time.monotonic() + motion["service_timeout_s"]
        cancel_sent = False
        while not result_future.done():
            if self.cancel_requested() and not cancel_sent:
                handle.cancel_goal_async()
                cancel_sent = True
                self.event(
                    self._active_order_id or "", "MOTION_CANCEL_REQUESTED", step=step
                )
            if time.monotonic() >= deadline:
                handle.cancel_goal_async()
                raise OrderError(
                    f"Manipulator {step} Action timed out; physical state is unknown"
                )
            time.sleep(0.05)
        wrapped = result_future.result()
        if wrapped.status == GoalStatus.STATUS_CANCELED:
            raise OrderCancelled(f"Manipulator {step} canceled at a motion boundary")
        if wrapped.status != GoalStatus.STATUS_SUCCEEDED or not wrapped.result.success:
            reason = wrapped.result.error or f"Manipulator {step} motion failed"
            if (
                step == "grab"
                and wrapped.status == GoalStatus.STATUS_ABORTED
                and getattr(wrapped.result, "safe_to_reselect", False)
            ):
                raise MotionPlanRejected(reason)
            raise OrderError(reason)

    @serialized_skill
    def pick_and_place(self, order: Order, detection_id: str) -> dict:
        """Re-localize a visual candidate, then execute validated ROS motion."""
        self.check_cancel()
        zone = self.config["zones"][order.zone]
        if detection_id not in self._visual_candidates:
            raise PerceptionMismatch(
                f"Unknown or stale visual detection {detection_id!r}; perceive again"
            )
        sku, observed_source = self._visual_candidates[detection_id]
        requested = {line.sku: line.quantity for line in order.lines}
        done = self._completed_by_sku.get(sku, 0)
        if sku not in requested or done >= requested[sku]:
            raise PerceptionMismatch(
                f"Visual detection {detection_id!r} is not needed; choose a requested SKU"
            )
        freshness = self.config.get("perception", {}).get("visual_freshness_s", 8.0)
        if time.monotonic() - self._visual_observed_at <= freshness:
            source = observed_source
        else:
            refreshed = self.detect(sku)
            matched = sorted(
                (
                    point
                    for point in refreshed
                    if not self._recently_rejected(sku, point)
                    and distance_xy(point, observed_source) <= self.inventory.radius
                ),
                key=lambda point: distance_xy(point, observed_source),
            )
            if not matched:
                self._visual_candidates.clear()
                raise PerceptionMismatch(
                    "Visual target disappeared or moved; perceive again",
                    code="TARGET_ASSOCIATION_LOST",
                )
            if (
                len(matched) > 1
                and (
                    distance_xy(matched[1], observed_source)
                    - distance_xy(matched[0], observed_source)
                )
                < 0.01
            ):
                self._visual_candidates.clear()
                raise PerceptionMismatch(
                    "Target identity is ambiguous; perceive again",
                    code="TARGET_ASSOCIATION_AMBIGUOUS",
                )
            source = matched[0]
        try:
            placement, target = self._placement_target(order, sku, source)
        except PerceptionMismatch:
            self._visual_candidates.pop(detection_id, None)
            raise
        if distance_xy(source, target) <= self._source_target_clearance():
            self._visual_candidates.pop(detection_id, None)
            self.event(
                order.order_id,
                "CANDIDATE_CLEARANCE_REJECTED",
                sku=sku,
                detection_id=detection_id,
                source=list(source),
                target=list(target),
                minimum_distance_m=self._source_target_clearance(),
            )
            raise PerceptionMismatch(
                "Source is too close to destination slot; no motion executed. "
                "Call get_object_positions again and choose another candidate"
            )
        self._choose_source([source], zone)
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
            distance_xy(actual, source) > self.inventory.radius
            or entity.prefab_name != sku
        ):
            code = (
                "POSITION_UNCONFIRMED"
                if distance_xy(actual, source) > self.inventory.radius
                else "CLASS_CONFLICT"
            )
            self._reject_visual_candidate(
                order,
                detection_id,
                sku,
                source,
                code,
                {"xy_error_m": distance_xy(actual, source)},
            )
            raise PerceptionMismatch(
                "Visual class/position failed safety check; choose another detection",
                code=code,
                reobserve=not bool(self._visual_candidates),
            )
        grasp_source = calibrated_grasp_point(source, sku, self.config)
        self._choose_source([grasp_source], zone)
        bin_index = placement.bin_index
        slot = tuple(zone["slots"][bin_index])
        slot_id = f"{order.zone}{bin_index + 1}"
        bin_position = self.slot_bin_position(zone, bin_index, slot)
        # Reserve from the fresh visual measurement, not from entity labels/poses.
        item_id = self._inventory_candidates.get(detection_id)
        if item_id is None:
            if self.require_order_reservation:
                raise PerceptionMismatch("Target is not part of this order reservation")
            item_id = self.inventory.observe(
                sku,
                self.config["camera"]["target_frame"],
                [source],
                complete_scan=False,
            )[0]
            self._inventory_candidates[detection_id] = item_id
        if self.require_order_reservation:
            try:
                self.inventory.select_for_order(item_id, order)
            except ValueError as exc:
                self._visual_candidates.pop(detection_id, None)
                raise PerceptionMismatch(
                    str(exc),
                    code="RESERVATION_CONFLICT",
                    reobserve=not bool(self._visual_candidates),
                ) from exc
        self.inventory.reserve(item_id, order.order_id, refreshed_position=source)
        self.event(order.order_id, "INVENTORY_RESERVED", item_id=item_id, sku=sku)
        self.event(
            order.order_id,
            "GRASPING",
            sku=sku,
            detection_id=detection_id,
            source=grasp_source,
            visual_source=source,
            position_source="rgbd_perception_with_workcell_calibration",
        )
        self.motion_attempted = True
        operation_id = getattr(
            self, "_operation_id", None
        ) or self.store.begin_operation(order.order_id, "PICK_AND_PLACE")
        self.inventory.begin_execution(item_id, order.order_id)
        self.physical_state_uncertain = True
        self._visual_observed_at = 0.0
        try:
            self.move(grasp_source, step="grab")
        except MotionPlanRejected as exc:
            self.physical_state_uncertain = False
            self._reject_visual_candidate(
                order, detection_id, sku, source, "MOTION_PLAN_REJECTED", {}
            )
            raise PerceptionMismatch(
                str(exc),
                code="MOTION_PLAN_REJECTED",
                reobserve=not bool(self._visual_candidates),
            ) from exc
        self.event(order.order_id, "LIFTING", sku=sku, detection_id=detection_id)
        self.move(
            grasp_source,
            step="lift",
            target_z=self.config["motion"]["post_grasp_lift_z_m"],
        )
        time.sleep(self.config["motion"]["lift_settle_time_s"])
        lifted = self.entity_position(entity)
        min_lift = self.config["motion"]["min_object_lift_m"]
        grasp_verified = lifted[2] - actual[2] >= min_lift
        self.event(
            order.order_id,
            "GRASP_CHECK",
            sku=sku,
            detection_id=detection_id,
            before=actual,
            after=lifted,
            min_lift_m=min_lift,
            verified=grasp_verified,
        )
        if not grasp_verified:
            raise OrderError(
                f"{sku} was not lifted after closing the gripper; "
                "stopping before transport"
            )
        self.inventory.transition(item_id, order.order_id, "CARRYING")
        self.event(order.order_id, "INVENTORY_CARRYING", item_id=item_id, sku=sku)
        carried_id = f"produce_workstation/carried_{entity.name}"
        self._planning_scene.attach_carried(
            carried_id,
            lifted,
            self.config["planning_scene"]["carried_radius_m"][sku],
            self.connector,
            self.config["camera"]["target_frame"],
        )
        self.event(order.order_id, "OBJECT_ATTACHED", sku=sku, object_id=carried_id)
        self.event(
            order.order_id,
            "PLACING",
            sku=sku,
            detection_id=detection_id,
            slot_id=slot_id,
            target=target,
            bin_position=bin_position,
        )
        self.move(
            (target[0], target[1], source[2]),
            step="place",
            target_z=self.config["motion"]["bin_release_z_m"],
        )
        self._planning_scene.detach_carried(carried_id)
        self.event(order.order_id, "OBJECT_DETACHED", sku=sku, object_id=carried_id)
        self.event(order.order_id, "RETRACTING", sku=sku, slot_id=slot_id)
        self.move(
            (target[0], target[1], source[2]),
            step="retreat",
            target_z=self.config["motion"]["bin_clearance_z_m"],
        )
        time.sleep(self.config["motion"]["settle_time_s"])
        bin_position = self.slot_bin_position(zone, bin_index, slot)
        self.event(order.order_id, "VERIFYING", sku=sku, slot_id=slot_id)
        if not verify_bin_placement(
            self.observe_after_place(sku, entity),
            source,
            bin_position,
            tuple(zone["bin_half_extents_m"]),
            self.config["workspace"]["source_exclusion_radius_m"],
        ):
            raise OrderError(f"{sku} was not observed inside bin {slot_id}")
        self.inventory.record_placement(
            order,
            item_id,
            entity.name,
            slot_id,
            source,
            target,
            operation_id,
            detection_id,
        )
        self.physical_state_uncertain = False
        self._needs_observation_motion = True
        self.event(
            order.order_id, "INVENTORY_PLACED", item_id=item_id, sku=sku, bin_id=slot_id
        )
        # This checkpoint proves that transport/placement/retreat completed.
        # A later perception failure must not erase that confirmed progress.
        self.physical_state_uncertain = False
        self._visual_candidates.clear()
        self._needs_observation_motion = True
        completed = sum(self._completed_by_sku.values())
        self.event(
            order.order_id,
            "ITEM_COMPLETED",
            sku=sku,
            detection_id=detection_id,
            slot_id=slot_id,
            completed=completed,
            total=order.total_items,
            sku_completed=done + 1,
            sku_requested=requested[sku],
            remaining_by_sku={
                line.sku: line.quantity - self._completed_by_sku.get(line.sku, 0)
                for line in order.lines
            },
        )
        if completed < order.total_items:
            self.check_cancel()
        if completed < order.total_items and self.yield_requested():
            self.event(
                order.order_id,
                "ORDER_YIELDING",
                completed=completed,
                total=order.total_items,
                reason="当前物品已装箱并退出，切换高优先级订单",
            )
            raise OrderPaused("已在单件完成的安全边界让出执行权")
        return {
            "placed": detection_id,
            "sku": sku,
            "bin": slot_id,
            "completed": completed,
            "next": "Call get_object_positions again before another pick",
        }

    @serialized_skill
    def finish_order(self, order: Order) -> dict:
        """The Agent can request completion, but cannot declare success itself."""
        self.check_cancel()
        for line in order.lines:
            if self._completed_by_sku.get(line.sku, 0) != line.quantity:
                raise OrderError(f"Order is incomplete: {line.sku}")
        zone = self.config["zones"][order.zone]
        self.event(
            order.order_id,
            "FINAL_AUDIT",
            completed=len(self._placed_entities),
            total=order.total_items,
        )
        # A failed physical audit must not be downgraded to a perception pause.
        self.physical_state_uncertain = True
        for sku, entity, bin_index, source in self._placed_entities:
            slot = tuple(zone["slots"][bin_index])
            bin_position = self.slot_bin_position(zone, bin_index, slot)
            if not verify_bin_placement(
                self.observe_after_place(sku, entity),
                source,
                bin_position,
                tuple(zone["bin_half_extents_m"]),
                self.config["workspace"]["source_exclusion_radius_m"],
            ):
                raise OrderError(
                    f"Final audit found {sku} outside bin {order.zone}{bin_index + 1}"
                )
        self.physical_state_uncertain = False
        return {"status": "verified", "completed": len(self._placed_entities)}

    def run(self, order: Order, *, agent_executor=None) -> dict:
        """Ask the model to solve a goal with tools, preserving a failure report."""
        failure = None
        self.event(order.order_id, "ORDER_STARTED", total=order.total_items)
        try:
            if not self._scene_prepared:
                raise OrderError("Agent execution requires prepare_scene=true")
            self.check_cancel()
            if agent_executor is not None:
                # Explicit test dependency injection; no in-process production fallback.
                agent_executor(order)
            else:
                if self._mcp_server is None or self._agent_dispatcher is None:
                    raise OrderError(
                        "Persistent MCP gateway and Agent dispatcher are not configured"
                    )
                with self._mcp_server.order_session(
                    order, allow_motion=self.execution_runner is None
                ) as server:
                    if self.execution_runner is not None:
                        self.execution_runner(order)
                    else:
                        self._agent_dispatcher.execute(order, server.url, server.token)
        except OrderPaused as exc:
            failure = str(exc)
            self.event(order.order_id, "ORDER_PAUSED", reason=failure)
            status = "PAUSED"
        except OrderCancelled as exc:
            failure = str(exc)
            self.inventory.finish_failed(order.order_id, self.motion_attempted)
            self.event(order.order_id, "ORDER_CANCELLED", reason=failure)
            status = "CANCELLED"
        except Exception as exc:
            failure = str(exc)
            if self._placed_entities and not self.physical_state_uncertain:
                failure = "[REVIEW_REQUIRED] " + failure
                self.event(
                    order.order_id,
                    "ORDER_PAUSED",
                    reason=failure,
                    next_action="视觉复查后继续；或明确丢弃部分订单",
                )
                status = "PAUSED"
            else:
                self.inventory.finish_failed(
                    order.order_id, self.physical_state_uncertain
                )
                self.event(order.order_id, "ORDER_FAILED", reason=failure)
                status = "FAILED"
        else:
            self.event(
                order.order_id, "ORDER_COMPLETED", completed=len(self._placed_entities)
            )
            status = "COMPLETED"
        return {
            "order_id": order.order_id,
            "status": status,
            "completed": len(self._placed_entities),
            "requested": order.total_items,
            "lines": [
                {"sku": line.sku, "quantity": line.quantity} for line in order.lines
            ],
            "error": failure,
            "events": self.events,
        }
