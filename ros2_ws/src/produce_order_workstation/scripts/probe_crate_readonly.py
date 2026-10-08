#!/usr/bin/env python3
"""Read-only planning diagnosis: no orders, motions or planning-scene writes.

Compare a hypothetical straight Cartesian transit with/without collision checks,
then identify crate contacts and ask OMPL for a safe alternative. This is not an
operator-order execution or a camera detection.
"""

import argparse
from copy import deepcopy
import json

import probe_obstacle as probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-x", type=float, default=.34)
    parser.add_argument("--source-y", type=float, default=.43)
    parser.add_argument("--target-x", type=float, default=.50)
    parser.add_argument("--target-y", type=float, default=-.48)
    parser.add_argument("--transit-z", type=float, default=.30)
    args = parser.parse_args()
    args.obstacle_x, args.obstacle_y = .43, 0.
    probe.rclpy.init()
    node = probe.rclpy.create_node("crate_readonly_diagnostic")
    clients = {
        "scene": node.create_client(probe.GetPlanningScene, "/get_planning_scene"),
        "ik": node.create_client(probe.GetPositionIK, "/compute_ik"),
        "cart": node.create_client(probe.GetCartesianPath, "/compute_cartesian_path"),
        "plan": node.create_client(probe.GetMotionPlan, "/plan_kinematic_path"),
        "fk": node.create_client(probe.GetPositionFK, "/compute_fk"),
        "valid": node.create_client(probe.GetStateValidity, "/check_state_validity"),
    }
    try:
        request = probe.GetPlanningScene.Request()
        request.components.components = (probe.PlanningSceneComponents.ROBOT_STATE |
                                         probe.PlanningSceneComponents.WORLD_OBJECT_GEOMETRY)
        scene = probe.call(node, clients["scene"], request).scene
        crate = next((obj for obj in scene.world.collision_objects
                      if obj.id == probe.PERMANENT_ID), None)
        if crate is None or len(crate.primitives) != 5:
            raise RuntimeError("Verified five-part return crate is not ready")
        ik = probe.GetPositionIK.Request()
        ik.ik_request.group_name = "panda_arm"
        ik.ik_request.robot_state = scene.robot_state
        ik.ik_request.ik_link_name = probe.LINK
        ik.ik_request.pose_stamped = probe.PoseStamped()
        ik.ik_request.pose_stamped.header.frame_id = probe.FRAME
        ik.ik_request.pose_stamped.pose = probe.pose(args.source_x, args.source_y, args.transit_z)
        ik.ik_request.timeout = probe.Duration(sec=3)
        ik.ik_request.avoid_collisions = True
        response = probe.call(node, clients["ik"], ik)
        if response.error_code.val != 1:
            raise RuntimeError(f"No safe source IK: {response.error_code.val}")
        state = response.solution
        cart = probe.GetCartesianPath.Request()
        cart.header.frame_id = probe.FRAME
        cart.start_state = state
        cart.group_name, cart.link_name = "panda_arm", probe.LINK
        cart.waypoints = [probe.pose(args.target_x, args.target_y, args.transit_z)]
        cart.max_step = .01
        cart.avoid_collisions = False
        unsafe = probe.call(node, clients["cart"], cart)
        contacts = set()
        trajectory = unsafe.solution.joint_trajectory
        for point in trajectory.points:
            waypoint = deepcopy(state)
            waypoint.joint_state.name = trajectory.joint_names
            waypoint.joint_state.position = point.positions
            check = probe.GetStateValidity.Request(robot_state=waypoint, group_name="panda_arm")
            validity = probe.call(node, clients["valid"], check)
            for contact in validity.contacts:
                pair = (contact.contact_body_1, contact.contact_body_2)
                if probe.PERMANENT_ID in pair:
                    contacts.add(pair)
        checked = probe.plan_pair(node, clients["cart"], clients["plan"], clients["fk"],
                                  clients["valid"], state, args)
        print(json.dumps({
            "executed": False, "scene_modified": False,
            "source": [args.source_x, args.source_y, args.transit_z],
            "target": [args.target_x, args.target_y, args.transit_z],
            "cartesian_collision_checks_disabled": unsafe.fraction,
            "cartesian_collision_checks_enabled": checked[0],
            "crate_contacts_on_unchecked_path": sorted(contacts),
            "ompl_error_code": checked[1], "ompl_points": checked[2],
        }, ensure_ascii=False, indent=2), flush=True)
    finally:
        node.destroy_node()
        probe.rclpy.shutdown()


if __name__ == "__main__":
    main()
