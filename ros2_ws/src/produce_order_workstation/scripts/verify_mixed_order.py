#!/usr/bin/env python3
"""Explicit integration test: one real mixed order through the public Action.

Uses the configured Agent API and physical simulation. No bypass of vision or
workstation safety checks. Does not collect goods or reset a scene by default.
"""

from datetime import datetime
import json
import time

import rclpy
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine


def main():
    rclpy.init()
    node = rclpy.create_node("mixed_order_integration_test")
    snapshots = []
    node.create_subscription(String, "/produce_orders/workcell_status",
                             lambda msg: snapshots.append(json.loads(msg.data)),
                             QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    deadline = time.monotonic() + 8
    while not snapshots and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.2)
    if not snapshots or not snapshots[-1].get("can_submit"):
        raise RuntimeError("Workcell not ready: test refused without resetting scene")
    client = ActionClient(node, ExecuteOrder, "/produce_orders/execute")
    if not client.wait_for_server(timeout_sec=5):
        raise RuntimeError("Order Action unavailable")
    goal = ExecuteOrder.Goal()
    goal.order_id = datetime.now().strftime("integration-%Y%m%d-%H%M%S")
    goal.zone, goal.prepare_scene = "A", True
    goal.lines = [OrderLine(sku=sku, quantity=1) for sku in ("corn", "carrot", "apple", "tomato")]
    def feedback(message):
        f = message.feedback
        if f.stage in ("ITEM_COMPLETED", "GRASPING", "PERCEPTION_EMPTY", "ORDER_FAILED", "ORDER_COMPLETED"):
            print(f"{f.stage} {f.completed}/{f.total} {f.detail_json}", flush=True)
    future = client.send_goal_async(goal, feedback_callback=feedback)
    rclpy.spin_until_future_complete(node, future, timeout_sec=10)
    handle = future.result()
    if handle is None or not handle.accepted:
        raise RuntimeError("Integration order rejected")
    print("ACCEPTED " + goal.order_id, flush=True)
    future = handle.get_result_async()
    deadline = time.monotonic() + 600
    while not future.done() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=.2)
    if not future.done():
        rclpy.spin_until_future_complete(node, handle.cancel_goal_async(), timeout_sec=5)
        raise RuntimeError("Integration test timed out; cancel requested, inspect scene")
    result = future.result().result
    print(json.dumps({"order_id": goal.order_id, "status": result.status,
                      "completed": result.completed, "requested": result.requested,
                      "error": result.error, "report": result.report_path}, indent=2), flush=True)
    client.destroy()
    node.destroy_node()
    rclpy.shutdown()
    if result.status != "COMPLETED" or result.completed != 4:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
