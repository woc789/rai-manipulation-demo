#!/usr/bin/env python3
"""Plan-only obstacle probe; never executes a robot motion."""

import argparse
from copy import deepcopy

from builtin_interfaces.msg import Duration
from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from moveit_msgs.msg import (
    AttachedCollisionObject,
    CollisionObject,
    Constraints,
    MotionPlanRequest,
    OrientationConstraint,
    PlanningScene,
    PlanningSceneComponents,
    PositionConstraint,
)
from moveit_msgs.srv import (
    ApplyPlanningScene,
    GetCartesianPath,
    GetMotionPlan,
    GetPlanningScene,
    GetPositionFK,
    GetPositionIK,
    GetStateValidity,
)
import rclpy
from shape_msgs.msg import SolidPrimitive


PROBE_ID = "produce_workstation/diagnostic_obstacle"
PERMANENT_ID = "produce_workstation/route_barrier"
FRAME = "panda_link0"
LINK = "panda_link8"
ORIENTATION = Quaternion(x=0.9238795325112867, y=-0.3826834323650898)


def call(node, client, request, timeout=12.0):
    if not client.wait_for_service(timeout_sec=timeout):
        raise RuntimeError(f"ROS service {client.srv_name} is unavailable")
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    if not future.done() or future.result() is None:
        raise RuntimeError(f"ROS service {client.srv_name} timed out")
    return future.result()


def pose(x, y, z):
    return Pose(position=Point(x=x, y=y, z=z), orientation=ORIENTATION)


def set_probe(node, apply_client, operation, args):
    collision = CollisionObject()
    collision.id = PROBE_ID
    collision.header.frame_id = FRAME
    collision.operation = operation
    if operation == CollisionObject.ADD:
        collision.pose.orientation.w = 1.0
        box = SolidPrimitive()
        box.type = SolidPrimitive.BOX
        box.dimensions = [args.width, args.depth, args.height]
        collision.primitives = [box]
        obstacle_pose = Pose()
        obstacle_pose.position = Point(
            x=args.obstacle_x, y=args.obstacle_y,
            z=args.bottom_z + args.height / 2,
        )
        obstacle_pose.orientation.w = 1.0
        collision.primitive_poses = [obstacle_pose]
    diff = PlanningScene()
    diff.is_diff = True
    diff.robot_state.is_diff = True
    diff.world.collision_objects = [collision]
    request = ApplyPlanningScene.Request()
    request.scene = diff
    if not call(node, apply_client, request).success:
        raise RuntimeError("MoveIt rejected the diagnostic obstacle")


def apply_object(node, apply_client, collision):
    diff = PlanningScene()
    diff.is_diff = True
    diff.robot_state.is_diff = True
    diff.world.collision_objects = [collision]
    request = ApplyPlanningScene.Request()
    request.scene = diff
    if not call(node, apply_client, request).success:
        raise RuntimeError(f"MoveIt rejected collision-object update for {collision.id}")


def plan_pair(node, cart_client, plan_client, fk_client, validity_client, state, args):
    above_source = pose(args.source_x, args.source_y, args.transit_z)
    above_target = pose(args.target_x, args.target_y, args.transit_z)
    cart = GetCartesianPath.Request()
    cart.header.frame_id = FRAME
    cart.start_state = state
    cart.group_name = "panda_arm"
    cart.link_name = LINK
    cart.waypoints = [above_source, above_target]
    cart.max_step = 0.01
    cart.avoid_collisions = True
    cart.max_velocity_scaling_factor = 1.0
    cart.max_acceleration_scaling_factor = 1.0
    cart_response = call(node, cart_client, cart, timeout=20.0)
    trajectory = cart_response.solution.joint_trajectory
    nearest = None
    for point in trajectory.points:
        waypoint_state = deepcopy(state)
        waypoint_state.joint_state.name = trajectory.joint_names
        waypoint_state.joint_state.position = point.positions
        fk_request = GetPositionFK.Request()
        fk_request.header.frame_id = FRAME
        fk_request.fk_link_names = [LINK]
        fk_request.robot_state = waypoint_state
        fk = call(node, fk_client, fk_request)
        xyz = fk.pose_stamped[0].pose.position
        distance = ((xyz.x - args.obstacle_x) ** 2 + (xyz.y - args.obstacle_y) ** 2) ** 0.5
        if nearest is None or distance < nearest[0]:
            nearest = (distance, waypoint_state, (xyz.x, xyz.y, xyz.z))
    validity_request = GetStateValidity.Request()
    validity_request.robot_state = nearest[1]
    validity_request.group_name = "panda_arm"
    validity = call(node, validity_client, validity_request)

    position = PositionConstraint()
    position.header.frame_id = FRAME
    position.link_name = LINK
    position.weight = 1.0
    region = SolidPrimitive()
    region.type = SolidPrimitive.SPHERE
    region.dimensions = [0.01]
    position.constraint_region.primitives = [region]
    position.constraint_region.primitive_poses = [above_target]
    orientation = OrientationConstraint()
    orientation.header.frame_id = FRAME
    orientation.link_name = LINK
    orientation.orientation = ORIENTATION
    orientation.absolute_x_axis_tolerance = 0.1
    orientation.absolute_y_axis_tolerance = 0.1
    orientation.absolute_z_axis_tolerance = 0.1
    orientation.weight = 1.0
    motion = MotionPlanRequest()
    motion.start_state = state
    motion.group_name = "panda_arm"
    motion.pipeline_id = "ompl"
    motion.num_planning_attempts = 3
    motion.allowed_planning_time = 5.0
    motion.max_velocity_scaling_factor = 1.0
    motion.max_acceleration_scaling_factor = 1.0
    motion.goal_constraints = [Constraints(
        position_constraints=[position], orientation_constraints=[orientation]
    )]
    request = GetMotionPlan.Request()
    request.motion_plan_request = motion
    plan_response = call(node, plan_client, request, timeout=15.0).motion_plan_response
    return cart_response.fraction, plan_response.error_code.val, len(
        plan_response.trajectory.joint_trajectory.points
    ), len(cart_response.start_state.attached_collision_objects), len(trajectory.points), (
        tuple(round(v, 3) for v in nearest[2]), round(nearest[0], 3), validity.valid
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-x", type=float, default=0.30)
    parser.add_argument("--source-y", type=float, default=0.33)
    parser.add_argument("--target-x", type=float, default=0.57)
    parser.add_argument("--target-y", type=float, default=-0.23)
    parser.add_argument("--obstacle-x", type=float, default=0.4625)
    parser.add_argument("--obstacle-y", type=float, default=0.0)
    parser.add_argument("--width", type=float, default=0.125)
    parser.add_argument("--depth", type=float, default=0.20)
    parser.add_argument("--bottom-z", type=float, default=0.01)
    parser.add_argument("--height", type=float, default=0.20)
    parser.add_argument("--transit-z", type=float, default=0.30)
    parser.add_argument(
        "--compare-temporary", action="store_true",
        help="Temporarily add another obstacle for a controlled idle-scene comparison",
    )
    parser.add_argument(
        "--replace-permanent", action="store_true",
        help="Temporarily hide the current route barrier during the comparison, then restore it",
    )
    args = parser.parse_args()
    rclpy.init()
    node = rclpy.create_node("workcell_obstacle_probe")
    get_client = node.create_client(GetPlanningScene, "/get_planning_scene")
    apply_client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    ik_client = node.create_client(GetPositionIK, "/compute_ik")
    cart_client = node.create_client(GetCartesianPath, "/compute_cartesian_path")
    plan_client = node.create_client(GetMotionPlan, "/plan_kinematic_path")
    fk_client = node.create_client(GetPositionFK, "/compute_fk")
    validity_client = node.create_client(GetStateValidity, "/check_state_validity")
    added = False
    original_barrier = None
    removed_barrier = False
    try:
        get_request = GetPlanningScene.Request()
        get_request.components.components = (
            PlanningSceneComponents.ROBOT_STATE |
            PlanningSceneComponents.WORLD_OBJECT_GEOMETRY
        )
        scene = call(node, get_client, get_request).scene
        if any(obj.id == PROBE_ID for obj in scene.world.collision_objects):
            raise RuntimeError("A diagnostic obstacle already exists; refusing to overwrite it")
        if args.replace_permanent:
            if not args.compare_temporary:
                raise ValueError("--replace-permanent requires --compare-temporary")
            original_barrier = next(
                (obj for obj in scene.world.collision_objects if obj.id == PERMANENT_ID), None
            )
            if original_barrier is None:
                raise RuntimeError("Current route barrier is missing from MoveIt")
            remove = CollisionObject()
            remove.id = PERMANENT_ID
            remove.header.frame_id = FRAME
            remove.operation = CollisionObject.REMOVE
            apply_object(node, apply_client, remove)
            removed_barrier = True
        ik = GetPositionIK.Request()
        ik.ik_request.group_name = "panda_arm"
        ik.ik_request.robot_state = scene.robot_state
        ik.ik_request.ik_link_name = LINK
        ik.ik_request.pose_stamped = PoseStamped()
        ik.ik_request.pose_stamped.header.frame_id = FRAME
        ik.ik_request.pose_stamped.pose = pose(args.source_x, args.source_y, 0.30)
        ik.ik_request.timeout = Duration(sec=3)
        ik.ik_request.avoid_collisions = True
        ik_response = call(node, ik_client, ik)
        if ik_response.error_code.val != 1:
            raise RuntimeError(f"Source pose has no valid IK solution: {ik_response.error_code.val}")
        start_state = ik_response.solution
        approach = GetCartesianPath.Request()
        approach.header.frame_id = FRAME
        approach.start_state = deepcopy(start_state)
        approach.group_name = "panda_arm"
        approach.link_name = LINK
        approach.waypoints = [pose(args.source_x, args.source_y, 0.135)]
        approach.max_step = 0.01
        approach.avoid_collisions = True
        approach_fraction = call(node, cart_client, approach).fraction
        carried = AttachedCollisionObject()
        carried.link_name = "panda_hand"
        carried.touch_links = ["panda_hand", "panda_leftfinger", "panda_rightfinger"]
        carried.object.id = "produce_workstation/diagnostic_carried"
        carried.object.header.frame_id = "panda_hand"
        carried.object.pose.orientation.w = 1.0
        carried.object.operation = CollisionObject.ADD
        sphere = SolidPrimitive()
        sphere.type = SolidPrimitive.SPHERE
        sphere.dimensions = [0.035]
        carried.object.primitives = [sphere]
        carried.object.primitive_poses = [pose(0.0, 0.0, 0.11)]
        start_state.attached_collision_objects.append(carried)
        baseline = plan_pair(
            node, cart_client, plan_client, fk_client, validity_client, start_state, args
        )
        if not args.compare_temporary:
            print(f"current scene: source approach={approach_fraction:.3f}, "
                  f"Cartesian transit={baseline[0]:.3f}, "
                  f"free-space code={baseline[1]}, points={baseline[2]}, "
                  f"attached={baseline[3]}, cart_points={baseline[4]}, "
                  f"nearest={baseline[5]}")
            return
        set_probe(node, apply_client, CollisionObject.ADD, args)
        added = True
        visible = call(node, get_client, get_request).scene.world.collision_objects
        if PROBE_ID not in {obj.id for obj in visible}:
            raise RuntimeError("Diagnostic obstacle was not visible in MoveIt planning scene")
        obstructed = plan_pair(
            node, cart_client, plan_client, fk_client, validity_client, start_state, args
        )
        print(f"without obstacle: Cartesian fraction={baseline[0]:.3f}, "
              f"free-space code={baseline[1]}, points={baseline[2]}, "
              f"attached={baseline[3]}, cart_points={baseline[4]}, "
              f"midpoint={baseline[5]}")
        print(f"with obstacle:    Cartesian fraction={obstructed[0]:.3f}, "
              f"free-space code={obstructed[1]}, points={obstructed[2]}, "
              f"attached={obstructed[3]}, cart_points={obstructed[4]}, "
              f"midpoint={obstructed[5]}")
    finally:
        try:
            if added:
                set_probe(node, apply_client, CollisionObject.REMOVE, args)
        finally:
            try:
                if removed_barrier:
                    original_barrier.operation = CollisionObject.ADD
                    apply_object(node, apply_client, original_barrier)
            finally:
                node.destroy_node()
                rclpy.shutdown()


if __name__ == "__main__":
    main()
