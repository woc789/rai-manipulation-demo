#!/usr/bin/env python3
"""Read-only collision checks for a saved diagnostic trajectory; never execute."""
import argparse
from collections import Counter
from copy import deepcopy
import json

import rclpy
from moveit_msgs.msg import RobotState
from moveit_msgs.srv import GetStateValidity
from rosidl_runtime_py.set_message import set_message_fields
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    args = parser.parse_args()
    with open(args.snapshot) as stream:
        snapshot = yaml.safe_load(stream)
    state = RobotState()
    set_message_fields(state, snapshot["scene"]["robot_state"])
    trajectory = snapshot["unchecked_trajectory"]["joint_trajectory"]
    rclpy.init()
    node = rclpy.create_node("saved_collision_inspection")
    client = node.create_client(GetStateValidity, "/check_state_validity")
    pairs = Counter()
    results = []
    try:
        if not client.wait_for_service(timeout_sec=10):
            raise RuntimeError("State validity service unavailable")
        for index, point in enumerate(trajectory["points"]):
            sample = deepcopy(state)
            sample.is_diff = True
            sample.joint_state.name = trajectory["joint_names"]
            sample.joint_state.position = point["positions"]
            sample.joint_state.velocity = []
            sample.joint_state.effort = []
            future = client.call_async(GetStateValidity.Request(robot_state=sample, group_name="panda_arm"))
            rclpy.spin_until_future_complete(node, future, timeout_sec=5)
            if not future.done():
                raise TimeoutError("State validity response timed out")
            result = future.result()
            contacts = sorted({tuple(sorted((c.contact_body_1, c.contact_body_2))) for c in result.contacts})
            pairs.update(contacts)
            results.append(dict(index=index, valid=result.valid, contacts=contacts))
        print(json.dumps(dict(note="CURRENT planning scene, saved joint samples; no execution",
                              samples=results, pairs={" / ".join(k): v for k, v in pairs.items()}), indent=2))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
