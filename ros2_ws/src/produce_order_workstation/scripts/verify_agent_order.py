#!/usr/bin/env python3
"""Explicit live check: register one real order and observe its report; never reset."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from produce_order_interfaces.msg import OrderLine
from produce_order_interfaces.srv import SubmitOrder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sku", choices=("corn", "carrot", "apple", "tomato"), default="carrot")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    identifier = datetime.now().strftime("agent-live-%Y%m%d-%H%M%S-%f")
    report = Path.home() / ".local/share/produce_order_workstation/reports" / f"{identifier}.json"
    rclpy.init()
    node = Node("produce_agent_live_check")
    workcell, agent = {}, {}
    qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
    node.create_subscription(String, "/produce_orders/workcell_status",
        lambda msg: workcell.update(json.loads(msg.data)), qos)
    node.create_subscription(String, "/produce_agent/status",
        lambda msg: agent.update(json.loads(msg.data)), qos)

    def event(msg):
        entry = json.loads(msg.data)
        if entry.get("order_id") == identifier and entry.get("stage") in {
            "MCP_CONNECTED", "AGENT_TOOL_CALL", "GRASP_CHECK", "ITEM_COMPLETED",
            "ORDER_COMPLETED", "ORDER_FAILED", "ORDER_PAUSED", "PERCEPTION_EMPTY"}:
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    node.create_subscription(String, "/produce_orders/events", event, 20)
    try:
        deadline = time.monotonic() + 10
        while (not workcell or not agent) and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.2)
        if workcell.get("state") != "READY" or agent.get("state") != "IDLE":
            raise RuntimeError(f"Refused: workcell={workcell.get('state')}, agent={agent.get('state')}")
        submit = node.create_client(SubmitOrder, "/produce_orders/submit")
        if not submit.wait_for_service(timeout_sec=5):
            raise RuntimeError("Order service unavailable")
        future = submit.call_async(SubmitOrder.Request(order_id=identifier, zone="A", priority=0,
            lines=[OrderLine(sku=args.sku, quantity=1)]))
        rclpy.spin_until_future_complete(node, future, timeout_sec=15)
        if not future.done():
            raise RuntimeError(f"Registration timed out; inspect {identifier}, do not repeat automatically")
        response = future.result()
        print(json.dumps({"order_id": identifier, "registered": response.success,
                          "message": response.message}, ensure_ascii=False), flush=True)
        if not response.success:
            raise RuntimeError("Order registration rejected")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.2)
            if report.is_file():
                try:
                    data = json.loads(report.read_text())
                except json.JSONDecodeError:
                    continue
                summary = {key: data[key] for key in ("order_id", "status", "completed", "requested", "error")}
                summary["report"] = str(report)
                print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
                if data["status"] != "COMPLETED" or data["completed"] != 1:
                    raise SystemExit(1)
                return
        raise RuntimeError(f"Timed out observing {identifier}; inspect active task before any retry")
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
