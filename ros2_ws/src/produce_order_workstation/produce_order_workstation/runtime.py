"""ROS 2 adapter and the sequential order workflow."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from rai.communication.ros2 import wait_for_ros2_services, wait_for_ros2_topics
from rai.communication.ros2.connectors import ROS2Connector
from rai.communication.ros2.ros_async import get_future_result
from rai_interfaces.srv import ManipulatorMoveTo
from rai_perception.tools import GetObjectPositionsTool
from rai_perception.tools.gripping_points_tools import GRIPPING_POINTS_TOOL_PARAM_PREFIX
from rai_sim.o3de.o3de_bridge import O3DExROS2Bridge
from rai_sim.simulation_bridge import SceneConfig
from std_msgs.msg import String

from .order import (
    Order,
    OrderError,
    choose_source,
    distance_xy,
    parse_centroids,
    verify_placement,
)


class Workstation:
    def __init__(self, connector: ROS2Connector, config: dict):
        self.connector = connector
        self.config = config
        self.publisher = connector.node.create_publisher(
            String, "/produce_orders/events", 10
        )
        self.events: list[dict] = []
        self._position_tool = None
        self._scene_bridge = None

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

    def wait_until_ready(self, with_scene: bool = False) -> None:
        camera = self.config["camera"]
        timeout = self.config["startup_timeout_s"]
        wait_for_ros2_topics(
            self.connector,
            [camera["color"], camera["depth"], camera["info"]],
            timeout=timeout,
        )
        services = ["/detection", "/segmentation", "/manipulator_move_to"]
        if with_scene:
            services += ["/spawn_entity", "/delete_entity"]
        wait_for_ros2_services(self.connector, services, timeout=timeout)
        for name, value in {
            "camera_topic": camera["color"],
            "depth_topic": camera["depth"],
            "camera_info_topic": camera["info"],
            "target_frame": camera["target_frame"],
            "source_frame": camera["source_frame"],
            "timeout_sec": self.config["perception_timeout_s"],
        }.items():
            self.connector.node.declare_parameter(
                f"{GRIPPING_POINTS_TOOL_PARAM_PREFIX}.{name}", value
            )
        self._position_tool = GetObjectPositionsTool(connector=self.connector)

    def prepare_scene(self, path: Path) -> None:
        """Populate the empty RoboticManipulationBenchmark level."""
        self._scene_bridge = O3DExROS2Bridge(self.connector)
        self._scene_bridge.setup_scene(SceneConfig.load_base_config(path))

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
        self, point: tuple[float, float, float], *, grip: str
    ) -> None:
        """Call the existing typed ROS service; stop on timeout or failure."""
        motion = self.config["motion"]
        request = ManipulatorMoveTo.Request()
        request.target_pose = PoseStamped()
        request.target_pose.header.frame_id = self.config["camera"]["target_frame"]
        extra_z = motion["grasp_z_offset_m"]
        if grip == "release":
            extra_z += motion["place_extra_height_m"]
        request.target_pose.pose = Pose(
            position=Point(
                x=point[0],
                y=point[1],
                z=max(point[2] + extra_z, motion["minimum_z_m"]),
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
        completed = 0
        used_slots = 0
        failure = None
        self.event(order.order_id, "ORDER_STARTED", total=order.total_items)
        try:
            for line in order.lines:
                for index in range(line.quantity):
                    slot = tuple(zone["slots"][used_slots])
                    self.event(
                        order.order_id,
                        "DETECTING",
                        sku=line.sku,
                        item=index + 1,
                        target=slot,
                    )
                    source, entity = self.locate_source(line.sku, zone)
                    if distance_xy(source, slot) <= 0.12:
                        raise OrderError("Source is too close to destination slot")
                    self.event(
                        order.order_id,
                        "GRASPING",
                        sku=line.sku,
                        source=source,
                        position_source="scene_tf" if entity else "perception",
                    )
                    self.move(source, grip="grab")
                    self.event(order.order_id, "PLACING", sku=line.sku, target=slot)
                    self.move((slot[0], slot[1], source[2]), grip="release")
                    time.sleep(self.config["motion"]["settle_time_s"])
                    self.event(order.order_id, "VERIFYING", sku=line.sku)
                    placed = verify_placement(
                        self.observe_after_place(line.sku, entity),
                        source,
                        slot,
                        zone["tolerance_m"],
                        self.config["workspace"]["source_exclusion_radius_m"],
                    )
                    if not placed:
                        raise OrderError(
                            f"{line.sku} was not observed at destination after release"
                        )
                    completed += 1
                    used_slots += 1
                    self.event(
                        order.order_id,
                        "ITEM_COMPLETED",
                        sku=line.sku,
                        completed=completed,
                        total=order.total_items,
                    )
        except (OrderError, TimeoutError, ValueError) as exc:
            failure = str(exc)
            self.event(order.order_id, "ORDER_FAILED", reason=failure)
        else:
            self.event(order.order_id, "ORDER_COMPLETED", completed=completed)
        return {
            "order_id": order.order_id,
            "status": "COMPLETED" if failure is None else "FAILED",
            "completed": completed,
            "requested": order.total_items,
            "error": failure,
            "events": self.events,
        }
