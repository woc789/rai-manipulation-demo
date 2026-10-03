"""Synchronize conservative workcell collision geometry with MoveIt."""

from __future__ import annotations

from math import acos, isfinite, sqrt

from geometry_msgs.msg import Point, Pose, Quaternion
from moveit_msgs.msg import (
    AttachedCollisionObject,
    CollisionObject,
    PlanningScene,
    PlanningSceneComponents,
)
from moveit_msgs.srv import ApplyPlanningScene, GetPlanningScene
from rai.communication.ros2.ros_async import get_future_result
from shape_msgs.msg import SolidPrimitive
from tf2_geometry_msgs import do_transform_pose


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
    barrier_pose: Pose,
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
    barrier = scene["barrier"]
    expected = barrier["expected_origin"]
    position = barrier_pose.position
    actual = (position.x, position.y, position.z)
    tolerance = _positive(barrier["pose_tolerance_m"], "barrier pose tolerance")
    if not all(isfinite(value) for value in actual) or any(
        abs(want - got) > tolerance for want, got in zip(expected, actual)
    ):
        raise ValueError(f"Physical route barrier moved from its expected pose: {actual}")
    q = barrier_pose.orientation
    norm = sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w)
    if not isfinite(norm) or abs(norm - 1.0) > 0.02:
        raise ValueError("Physical route barrier has an invalid orientation")
    dot = abs((q.y + q.w) / (sqrt(2) * norm))
    angle = 2 * acos(min(1.0, dot))
    if angle > _positive(barrier["orientation_tolerance_rad"], "barrier orientation tolerance"):
        raise ValueError(f"Physical route barrier tipped over: {angle:.3f} rad")
    dimensions = tuple(
        _positive(value, "barrier dimension") for value in barrier["dimensions_m"]
    )
    if len(dimensions) != 3 or len(barrier["center_offset_m"]) != 3:
        raise ValueError("Route barrier geometry needs three dimensions and offsets")
    obstacle = CollisionObject()
    obstacle.id = OBJECT_PREFIX + barrier["entity"]
    obstacle.header.frame_id = frame
    obstacle.pose.orientation.w = 1.0
    obstacle.operation = CollisionObject.ADD
    center = tuple(a + float(b) for a, b in zip(actual, barrier["center_offset_m"]))
    _box_at(obstacle, center, dimensions)
    objects.append(obstacle)
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
        self._carried_id = None

    def _read_scene(self, components: int) -> PlanningScene:
        request = GetPlanningScene.Request()
        request.components.components = components
        response = get_future_result(
            self._get.call_async(request), timeout_sec=self.timeout_s
        )
        if response is None:
            raise RuntimeError("Timed out reading the MoveIt planning scene")
        return response.scene

    def _apply_diff(self, diff: PlanningScene) -> None:
        diff.is_diff = True
        diff.robot_state.is_diff = True
        request = ApplyPlanningScene.Request()
        request.scene = diff
        response = get_future_result(
            self._apply.call_async(request), timeout_sec=self.timeout_s
        )
        if response is None or not response.success:
            raise RuntimeError("MoveIt rejected or timed out applying the planning scene")

    def _current_objects(self) -> dict[str, CollisionObject]:
        scene = self._read_scene(PlanningSceneComponents.WORLD_OBJECT_GEOMETRY)
        return {obj.id: obj for obj in scene.world.collision_objects}

    def _current_attached(self) -> dict[str, AttachedCollisionObject]:
        scene = self._read_scene(PlanningSceneComponents.ROBOT_STATE_ATTACHED_OBJECTS)
        return {item.object.id: item for item in scene.robot_state.attached_collision_objects}

    def attach_carried(
        self,
        object_id: str,
        world_position: tuple[float, float, float],
        radius_m: float,
        connector,
        world_frame: str,
    ) -> None:
        """Use the verified lifted pose as a simulation-only grasp geometry oracle."""
        if not object_id.startswith(OBJECT_PREFIX + "carried_"):
            raise ValueError("Carried object ID is outside the workstation namespace")
        if self._carried_id is not None:
            raise RuntimeError("A carried object is already attached")
        radius = _positive(radius_m, "carried-object radius")
        if not all(isfinite(float(value)) for value in world_position):
            raise ValueError("Invalid carried-object position")
        transform = connector.get_transform("panda_hand", world_frame, timeout_sec=self.timeout_s)
        local_pose = do_transform_pose(
            Pose(
                position=Point(x=world_position[0], y=world_position[1], z=world_position[2]),
                orientation=Quaternion(w=1.0),
            ),
            transform,
        )
        primitive = SolidPrimitive()
        primitive.type = SolidPrimitive.SPHERE
        primitive.dimensions = [radius]
        collision = CollisionObject()
        collision.id = object_id
        collision.header.frame_id = "panda_hand"
        collision.pose.orientation.w = 1.0
        collision.operation = CollisionObject.ADD
        collision.primitives = [primitive]
        collision.primitive_poses = [local_pose]
        attached = AttachedCollisionObject()
        attached.link_name = "panda_hand"
        attached.object = collision
        attached.touch_links = ["panda_hand", "panda_leftfinger", "panda_rightfinger"]
        diff = PlanningScene()
        diff.robot_state.attached_collision_objects.append(attached)
        self._apply_diff(diff)
        observed = self._current_attached().get(object_id)
        if observed is None or observed.link_name != "panda_hand":
            raise RuntimeError("MoveIt did not confirm the carried-object attachment")
        self._carried_id = object_id

    def detach_carried(self, object_id: str) -> None:
        if object_id != self._carried_id:
            raise RuntimeError("The requested carried object is not attached")
        diff = PlanningScene()
        removal = AttachedCollisionObject()
        removal.link_name = "panda_hand"
        removal.object.id = object_id
        removal.object.operation = CollisionObject.REMOVE
        diff.robot_state.attached_collision_objects.append(removal)
        world_removal = CollisionObject()
        world_removal.id = object_id
        world_removal.operation = CollisionObject.REMOVE
        diff.world.collision_objects.append(world_removal)
        self._apply_diff(diff)
        if object_id in self._current_attached() or object_id in self._current_objects():
            raise RuntimeError("MoveIt did not remove the carried-object geometry")
        self._carried_id = None

    def synchronize(
        self, config: dict, bin_positions: dict[str, tuple[float, float, float]],
        barrier_pose: Pose,
    ) -> list[str]:
        desired = build_collision_objects(config, bin_positions, barrier_pose)
        if not self._apply.wait_for_service(timeout_sec=self.timeout_s):
            raise RuntimeError("MoveIt apply_planning_scene service is unavailable")
        if not self._get.wait_for_service(timeout_sec=self.timeout_s):
            raise RuntimeError("MoveIt get_planning_scene service is unavailable")
        existing = self._current_objects()
        attached = self._current_attached()
        desired_ids = {obj.id for obj in desired}
        stale_ids = {
            obj_id for obj_id in existing
            if obj_id.startswith(OBJECT_PREFIX) and obj_id not in desired_ids
        }
        diff = PlanningScene()
        for obj_id in attached:
            if obj_id.startswith(OBJECT_PREFIX + "carried_"):
                removal = AttachedCollisionObject()
                removal.link_name = attached[obj_id].link_name
                removal.object.id = obj_id
                removal.object.operation = CollisionObject.REMOVE
                diff.robot_state.attached_collision_objects.append(removal)
        for obj_id in sorted(stale_ids):
            removal = CollisionObject()
            removal.id = obj_id
            removal.header.frame_id = config["camera"]["target_frame"]
            removal.operation = CollisionObject.REMOVE
            diff.world.collision_objects.append(removal)
        diff.world.collision_objects.extend(desired)
        self._apply_diff(diff)
        observed = self._current_objects()
        if any(
            obj.id not in observed or not _geometry_matches(obj, observed[obj.id])
            for obj in desired
        ) or any(obj_id in observed for obj_id in stale_ids) or any(
            obj_id.startswith(OBJECT_PREFIX + "carried_")
            for obj_id in self._current_attached()
        ):
            raise RuntimeError("MoveIt planning scene did not match the workcell geometry")
        self._carried_id = None
        return [obj.id for obj in desired]
