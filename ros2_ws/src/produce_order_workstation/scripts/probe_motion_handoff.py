#!/usr/bin/env python3
"""Empty-hand motion handoff check. Plan-only unless --execute is explicit.

Never submits an order, calls an LLM, picks stock, or resets the scene. Refuses
active orders, attached objects and non-clearance starting poses. Executed
targets stay in the same XY column, at least 27 cm above the base origin.
"""

import argparse
from copy import deepcopy
import json
from time import monotonic

from moveit_msgs.msg import PlanningSceneComponents
from moveit_msgs.srv import GetCartesianPath, GetPlanningScene, GetPositionFK
from produce_order_interfaces.action import ExecuteMotion
import rclpy
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String


def wait(node, future, timeout=12.0):
    rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
    if not future.done():
        raise RuntimeError("ROS response timed out; stop diagnostic")
    return future.result()


def call(node, service_type, name, request):
    client = node.create_client(service_type, name)
    if not client.wait_for_service(timeout_sec=5.0):
        raise RuntimeError(f"Service unavailable: {name}")
    return wait(node, client.call_async(request))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    rclpy.init()
    node = rclpy.create_node("motion_handoff_diagnostic")
    statuses = []
    node.create_subscription(String, "/produce_orders/workcell_status",
                             lambda m: statuses.append(json.loads(m.data)),
                             QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    try:
        status_deadline = monotonic() + 8.0
        while monotonic() < status_deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            if statuses:
                break
        if not statuses or statuses[-1]["state"] not in ("READY", "RECOVERY_REQUIRED"):
            raise RuntimeError(f"Workcell is not idle/synchronized; diagnostic refused: {statuses[-1]['state'] if statuses else 'NO_STATUS'}")
        request = GetPlanningScene.Request()
        request.components.components = PlanningSceneComponents.ROBOT_STATE | PlanningSceneComponents.ROBOT_STATE_ATTACHED_OBJECTS
        state = call(node, GetPlanningScene, "/get_planning_scene", request).scene.robot_state
        if state.attached_collision_objects:
            raise RuntimeError("Carried object exists; empty-hand diagnostic refused")
        fk = GetPositionFK.Request()
        fk.header.frame_id = "panda_link0"
        fk.fk_link_names = ["panda_link8"]
        fk.robot_state = state
        response = call(node, GetPositionFK, "/compute_fk", fk)
        if response.error_code.val != 1:
            raise RuntimeError("Could not obtain current end-effector pose")
        start = response.pose_stamped[0]
        p = start.pose.position
        if not (0.20 <= p.x <= 0.60 and abs(p.y) <= 0.55 and 0.26 <= p.z <= 0.45):
            raise RuntimeError(f"Current pose is outside safe probe clearance: {p}")
        targets = [deepcopy(start) for _ in range(3)]
        for target, z in zip(targets, (0.28, 0.27, 0.28)):
            target.pose.position.z = z
        cart = GetCartesianPath.Request()
        cart.header.frame_id = "panda_link0"
        cart.start_state = state
        cart.group_name, cart.link_name = "panda_arm", "panda_link8"
        cart.waypoints = [target.pose for target in targets]
        cart.max_step, cart.avoid_collisions = 0.01, True
        planned = call(node, GetCartesianPath, "/compute_cartesian_path", cart)
        if planned.fraction < 0.999:
            raise RuntimeError(f"Probe path not collision-free: {planned.fraction}")
        results = []
        if args.execute:
            client = ActionClient(node, ExecuteMotion, "/produce_orders/execute_motion")
            if not client.wait_for_server(timeout_sec=5):
                raise RuntimeError("Motion Action unavailable")
            for target in targets:
                goal = ExecuteMotion.Goal()
                goal.target_pose = target
                goal.step = "smoke"
                goal.initial_gripper_state = goal.final_gripper_state = True
                handle = wait(node, client.send_goal_async(goal), 5)
                if not handle.accepted:
                    raise RuntimeError("Probe motion rejected")
                try:
                    result = wait(node, handle.get_result_async(), 45).result
                except RuntimeError:
                    wait(node, handle.cancel_goal_async(), 5)
                    raise
                results.append({"success": result.success, "error": result.error})
                if not result.success:
                    break
        print(json.dumps({"order_submitted": False, "scene_reset": False,
                          "executed": args.execute, "collision_checked_fraction": planned.fraction,
                          "start": [p.x, p.y, p.z], "motion_results": results}, indent=2))
        if args.execute and (len(results) != 3 or not all(r["success"] for r in results)):
            raise RuntimeError("Motion handoff diagnostic failed")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
