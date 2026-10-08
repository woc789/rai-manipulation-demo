#!/usr/bin/env python3
"""Explicit integration test: one real mixed order through the public Action.

Uses the configured Agent API and physical simulation. No bypass of vision or
workstation safety checks. Does not collect goods or reset a scene by default.
"""

from datetime import datetime
import argparse
import json
import time
from math import degrees
from pathlib import Path

import rclpy
from rclpy.action import ActionClient
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine
from sensor_msgs.msg import JointState
from rclpy.qos import ReliabilityPolicy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skus", nargs="+", choices=("corn", "carrot", "apple", "tomato"),
                        default=["corn", "carrot", "apple", "tomato"])
    skus = parser.parse_args().skus
    if len(skus) != len(set(skus)):
        parser.error("Each SKU may appear once; this diagnostic requests one item per SKU")
    rclpy.init()
    node = rclpy.create_node("mixed_order_integration_test")
    snapshots = []
    # Real joint feedback, grouped by Action stage. Never infer motion from goals.
    stage = ["before_order"]
    measured = {}
    def joints(message):
        values = dict(zip(message.name, message.position))
        names = [f"panda_joint{i}" for i in range(1, 8)]
        if not all(name in values for name in names):
            return
        point = [values[name] for name in names]
        keys = [stage[0]]
        if ":" in stage[0]:
            keys += ["item:" + stage[0].split(":", 1)[0], "whole_order"]
        for key in keys:
            row = measured.setdefault(key, {"samples": 0, "minimum": point[:], "maximum": point[:], "travel": [0.] * 7, "last": point[:]})
            for i, value in enumerate(point):
                row["minimum"][i] = min(row["minimum"][i], value)
                row["maximum"][i] = max(row["maximum"][i], value)
                row["travel"][i] += abs(value - row["last"][i])
            row["last"] = point
            row["samples"] += 1
    node.create_subscription(JointState, "/joint_states", joints,
                             QoSProfile(depth=100, reliability=ReliabilityPolicy.BEST_EFFORT))
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
    goal.lines = [OrderLine(sku=sku, quantity=1) for sku in skus]
    def feedback(message):
        f = message.feedback
        entry = json.loads(f.detail_json)
        if f.stage == "MOTION_STAGE":
            stage[0] = f"{f.completed}:{entry.get('motion_stage', '')}"
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
    audit = {}
    for key, row in measured.items():
        audit[key] = {"samples": row["samples"],
                      "minimum_degrees": [round(degrees(v), 2) for v in row["minimum"]],
                      "maximum_degrees": [round(degrees(v), 2) for v in row["maximum"]],
                      "range_degrees": [round(degrees(b-a), 2) for a,b in zip(row["minimum"], row["maximum"])],
                      "travel_degrees": [round(degrees(v), 2) for v in row["travel"]]}
    report_path = Path(result.report_path)
    audit_path = report_path.parent / "motion" / report_path.name
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps({"order_id": goal.order_id, "source": "/joint_states", "stages": audit}, indent=2) + "\n")
    print("REAL JOINT AUDIT " + str(audit_path), flush=True)
    print(json.dumps({"order_id": goal.order_id, "status": result.status,
                      "completed": result.completed, "requested": result.requested,
                      "error": result.error, "report": result.report_path}, indent=2), flush=True)
    client.destroy()
    node.destroy_node()
    rclpy.shutdown()
    if result.status != "COMPLETED" or result.completed != len(skus):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
