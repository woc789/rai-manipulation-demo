#!/usr/bin/env python3
"""Execute an empty-gripper diagnostic, recording actual joint-state FK, not a plan.

Run only on an idle, READY workstation. Does not submit an order or grasp inventory.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import time

from geometry_msgs.msg import Point, PoseStamped, Quaternion
from moveit_msgs.msg import PlanningSceneComponents
from moveit_msgs.srv import GetPlanningScene, GetPositionFK
import rclpy
from rclpy.action import ActionClient
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import JointState
from std_msgs.msg import String

from produce_order_interfaces.action import ExecuteMotion
from probe_obstacle import call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/workstation_obstacle_trace.json")
    parser.add_argument("--observe-seconds", type=float, default=0,
                        help="Read-only recording of an operator's order; sends no motion goals")
    args = parser.parse_args()
    rclpy.init()
    node = rclpy.create_node("actual_obstacle_motion_trace")
    samples = []
    status = {}
    recording = False
    last_sample = 0.0

    def on_status(message):
        status.update(json.loads(message.data))

    def on_joints(message):
        nonlocal last_sample
        now = time.monotonic()
        if recording and now - last_sample >= 0.08:
            samples.append((now, deepcopy(message)))
            last_sample = now

    node.create_subscription(String, "/produce_orders/workcell_status", on_status, 10)
    node.create_subscription(JointState, "/joint_states", on_joints, qos_profile_sensor_data)
    deadline = time.monotonic() + 10
    while not status and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.2)
    if not args.observe_seconds and status.get("state") != "READY":
        raise RuntimeError(f"Workstation must be READY: {status}")
    client = ActionClient(node, ExecuteMotion, "/produce_orders/execute_motion")
    if not client.wait_for_server(timeout_sec=10):
        raise RuntimeError("Motion Action unavailable")

    def move(x, y, z):
        goal = ExecuteMotion.Goal()
        goal.target_pose = PoseStamped()
        goal.target_pose.header.frame_id = "panda_link0"
        goal.target_pose.pose.position = Point(x=x, y=y, z=z)
        goal.target_pose.pose.orientation = Quaternion(x=0.9238795325, y=-0.3826834324)
        goal.initial_gripper_state = goal.final_gripper_state = True
        goal.step = "smoke"
        future = client.send_goal_async(goal)
        rclpy.spin_until_future_complete(node, future, timeout_sec=15)
        if not future.done() or not future.result().accepted:
            raise RuntimeError("Motion goal not accepted")
        handle = future.result()
        result = handle.get_result_async()
        rclpy.spin_until_future_complete(node, result, timeout_sec=120)
        if not result.done():
            handle.cancel_goal_async()
            raise RuntimeError("Motion timed out; cancellation requested")
        if not result.result().result.success:
            raise RuntimeError(result.result().result.error)

    try:
        # Above apple_1 -> A3, without touching stock or submitting an order.
        if not args.observe_seconds:
            move(0.253815, -0.167219, 0.30)
        scene_client = node.create_client(GetPlanningScene, "/get_planning_scene")
        request = GetPlanningScene.Request()
        request.components.components = PlanningSceneComponents.ROBOT_STATE
        state = call(node, scene_client, request).scene.robot_state
        recording = True
        if args.observe_seconds:
            deadline = time.monotonic() + args.observe_seconds
            while time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=0.1)
        else:
            move(0.57, 0.23, 0.32)
        recording = False
        fk_client = node.create_client(GetPositionFK, "/compute_fk")
        points = []
        for stamp, joints in samples:
            request = GetPositionFK.Request()
            request.header.frame_id = "panda_link0"
            request.fk_link_names = ["panda_link8"]
            request.robot_state = deepcopy(state)
            request.robot_state.joint_state = joints
            reply = call(node, fk_client, request)
            if reply.error_code.val != 1:
                raise RuntimeError("FK failed for an actual joint-state sample")
            p = reply.pose_stamped[0].pose.position
            points.append([stamp - samples[0][0], p.x, p.y, p.z])
        if len(points) < 2:
            raise RuntimeError("Insufficient actual joint-state samples")
        start, end = points[0][1:], points[-1][1:]
        delta = [b - a for a, b in zip(start, end)]
        length2 = max(1e-12, sum(v * v for v in delta))
        deviations = []
        for point in points:
            p = point[1:]
            u = max(0, min(1, sum((v - a) * d for v, a, d in zip(p, start, delta)) / length2))
            deviations.append(sum((v - (a + u * d)) ** 2 for v, a, d in zip(p, start, delta)) ** 0.5)
        report = {"kind": "observed_joint_state_fk" if args.observe_seconds else "executed_empty_gripper_joint_state_fk", "order_submitted": False,
                  "frame": "panda_link0", "link": "panda_link8", "points_txyz": points,
                  "max_deviation_from_straight_m": max(deviations),
                  "z_range_m": [min(p[3] for p in points), max(p[3] for p in points)]}
        if args.observe_seconds:
            # A whole order is not one transfer: do not call its overall spread
            # an obstacle-avoidance deviation.
            report.pop("max_deviation_from_straight_m")
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in report.items() if k != "points_txyz"}, indent=2))
        print(f"Actual trace: {args.output} ({len(points)} samples)")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
