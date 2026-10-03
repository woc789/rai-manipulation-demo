"""Synchronize conservative workcell collision geometry with MoveIt."""

from __future__ import annotations

from math import isfinite

from geometry_msgs.msg import Point, Pose, Quaternion
from moveit_msgs.msg import CollisionObject, PlanningScene, PlanningSceneComponents
from moveit_msgs.srv import ApplyPlanningScene, GetPlanningScene
from rai.communication.ros2.ros_async import get_future_result
from shape_msgs.msg import SolidPrimitive


OBJECT_PREFIX = "produce_workstation/"


def _positive(value: float, label: str) -> float:
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError(f"Invalid planning-scene {label}: {value!r}")
    return number


def _box_at(
    obj: CollisionObject,
    center: tuple[float, float, float],
    dimensions: tuple[float, float, float],
) -> None:
    if not all(isfinite(float(value)) for value in center):
        raise ValueError(f"Non-finite collision-object position for {obj.id}")
    primitive = SolidPrimitive()
    primitive.type = SolidPrimitive.BOX
    primitive.dimensions = [
        _positive(value, f"{obj.id} dimension") for value in dimensions
    ]
    obj.primitives.append(primitive)
    obj.primitive_poses.append(
        Pose(position=Point(x=center[0], y=center[1], z=center[2]),
             orientation=Quaternion(w=1.0))
    )


def build_collision_objects(
    config: dict,
    bin_positions: dict[str, tuple[float, float, float]],
) -> list[CollisionObject]:
    """Create a low table slab and open-top bin proxies, not stock objects.

    These are deliberately conservative approximations. The tabletop height
    and bin dimensions are explicit configuration, not inferred from the
    simulator's entity origin or the RGB-D target coordinates.
    """
    scene = config["planning_scene"]
    frame = config["camera"]["target_frame"]
    if not frame:
        raise ValueError("Planning-scene frame is empty")
    x_min, x_max = map(float, config["workspace"]["x"])
    y_min, y_max = map(float, config["workspace"]["y"])
    if not all(map(isfinite, (x_min, x_max, y_min, y_max))) or x_min >= x_max or y_min >= y_max:
        raise ValueError("Invalid workcell bounds for planning scene")

    margin = float(scene["table_xy_margin_m"])
    thickness = _positive(scene["table_thickness_m"], "table thickness")
    table_top = float(scene["table_top_z_m"])
    if not isfinite(margin) or margin < 0 or not isfinite(table_top):
        raise ValueError("Invalid planning-scene table geometry")
    table = CollisionObject()
    table.id = OBJECT_PREFIX + "table"
    table.header.frame_id = frame
    table.pose.orientation.w = 1.0
    table.operation = CollisionObject.ADD
    _box_at(
        table,
        ((x_min + x_max) / 2, (y_min + y_max) / 2, table_top - thickness / 2),
        (x_max - x_min + 2 * margin, y_max - y_min + 2 * margin, thickness),
    )
    objects = [table]

    floor_thickness = _positive(scene["bin_floor_thickness_m"], "bin floor thickness")
    wall_thickness = _positive(scene["bin_wall_thickness_m"], "bin wall thickness")
    wall_height = _positive(scene["bin_wall_height_m"], "bin wall height")
    floor_z_offset = float(scene["bin_floor_z_offset_m"])
    if not isfinite(floor_z_offset):
        raise ValueError("Invalid bin floor offset")
    expected_names = {
        name for zone in config["zones"].values() for name in zone["slot_entities"]
    }
    if set(bin_positions) != expected_names:
        raise ValueError("Planning-scene bin poses do not match configured slots")
    for zone in config["zones"].values():
        half_x, half_y = (_positive(value, "bin half extent") for value in zone["bin_half_extents_m"])
        if wall_thickness >= min(half_x, half_y):
            raise ValueError("Bin collision wall is too thick for the configured opening")
        for name in zone["slot_entities"]:
            x, y, entity_z = bin_positions[name]
            if not all(map(isfinite, (x, y, entity_z))):
                raise ValueError(f"Invalid simulator pose for {name}")
            floor_top = entity_z + floor_z_offset
            obj = CollisionObject()
            obj.id = OBJECT_PREFIX + name
            obj.header.frame_id = frame
            obj.pose.orientation.w = 1.0
            obj.operation = CollisionObject.ADD
            _box_at(obj, (x, y, floor_top - floor_thickness / 2),
                    (2 * half_x, 2 * half_y, floor_thickness))
            wall_z = floor_top + wall_height / 2
            wall_x = half_x - wall_thickness / 2
            wall_y = half_y - wall_thickness / 2
            for side in (-1, 1):
                _box_at(obj, (x + side * wall_x, y, wall_z),
                        (wall_thickness, 2 * half_y, wall_height))
                _box_at(obj, (x, y + side * wall_y, wall_z),
                        (2 * half_x, wall_thickness, wall_height))
            objects.append(obj)
    return objects


def _geometry_matches(expected: CollisionObject, actual: CollisionObject) -> bool:
    """Compare world-space box centres; MoveIt may normalize object poses."""
    if len(expected.primitives) != len(actual.primitives):
        return False
    for wanted, got, wanted_pose, got_pose in zip(
        expected.primitives, actual.primitives,
        expected.primitive_poses, actual.primitive_poses,
    ):
        if wanted.type != got.type or len(wanted.dimensions) != len(got.dimensions):
            return False
        if any(abs(a - b) > 0.001 for a, b in zip(wanted.dimensions, got.dimensions)):
            return False
        for axis in ("x", "y", "z"):
            wanted_position = (
                getattr(expected.pose.position, axis) + getattr(wanted_pose.position, axis)
            )
            got_position = getattr(actual.pose.position, axis) + getattr(got_pose.position, axis)
            if abs(wanted_position - got_position) > 0.001:
                return False
    return True


class PlanningSceneSync:
    """Own only produce_workstation/* objects in move_group's planning scene."""

    def __init__(self, node, timeout_s: float = 10.0):
        self.timeout_s = _positive(timeout_s, "service timeout")
        self._apply = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
        self._get = node.create_client(GetPlanningScene, "/get_planning_scene")

    def _current_objects(self) -> dict[str, CollisionObject]:
        request = GetPlanningScene.Request()
        request.components.components = PlanningSceneComponents.WORLD_OBJECT_GEOMETRY
        response = get_future_result(
            self._get.call_async(request), timeout_sec=self.timeout_s
        )
        if response is None:
            raise RuntimeError("Timed out reading the MoveIt planning scene")
        return {obj.id: obj for obj in response.scene.world.collision_objects}

    def synchronize(self, config: dict, bin_positions: dict[str, tuple[float, float, float]]) -> list[str]:
        desired = build_collision_objects(config, bin_positions)
        if not self._apply.wait_for_service(timeout_sec=self.timeout_s):
            raise RuntimeError("MoveIt apply_planning_scene service is unavailable")
        if not self._get.wait_for_service(timeout_sec=self.timeout_s):
            raise RuntimeError("MoveIt get_planning_scene service is unavailable")
        existing = self._current_objects()
        desired_ids = {obj.id for obj in desired}
        stale_ids = {
            obj_id for obj_id in existing
            if obj_id.startswith(OBJECT_PREFIX) and obj_id not in desired_ids
        }
        diff = PlanningScene()
        diff.is_diff = True
        diff.robot_state.is_diff = True
        for obj_id in sorted(stale_ids):
            removal = CollisionObject()
            removal.id = obj_id
            removal.header.frame_id = config["camera"]["target_frame"]
            removal.operation = CollisionObject.REMOVE
            diff.world.collision_objects.append(removal)
        diff.world.collision_objects.extend(desired)
        request = ApplyPlanningScene.Request()
        request.scene = diff
        response = get_future_result(
            self._apply.call_async(request), timeout_sec=self.timeout_s
        )
        if response is None or not response.success:
            raise RuntimeError("MoveIt rejected or timed out applying the planning scene")
        observed = self._current_objects()
        if any(
            obj.id not in observed or not _geometry_matches(obj, observed[obj.id])
            for obj in desired
        ) or any(obj_id in observed for obj_id in stale_ids):
            raise RuntimeError("MoveIt planning scene did not match the workcell geometry")
        return [obj.id for obj in desired]
