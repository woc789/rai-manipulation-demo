#!/usr/bin/env python3
"""Explicit live orders through SubmitOrder, without resets or truth-based targeting.

Example: --order corn=2 --order carrot=1 --order apple=1,tomato=1
Completed boxes remain in the scene. Partial review or uncertain failures stop
the batch, rather than automatically resuming, collecting, or resetting it.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from produce_order_interfaces.msg import OrderLine
from produce_order_interfaces.srv import SubmitOrder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order", action="append", required=True)
    parser.add_argument("--ready-timeout", type=float, default=480)
    parser.add_argument("--order-timeout", type=float, default=360)
    args = parser.parse_args()
    demands = []
    for spec in args.order:
        lines = []
        for item in spec.split(","):
            sku, quantity = item.split("=", 1)
            count = int(quantity)
            if not sku or count < 1:
                parser.error("Each item must be sku=positive_integer")
            lines.append(OrderLine(sku=sku, quantity=count))
        demands.append(lines)
    rclpy.init()
    node = rclpy.create_node("perception_logic_live_verification")
    state = [{}]
    updated = [0.]
    events = []

    def status(msg):
        state[0] = json.loads(msg.data)
        updated[0] = time.monotonic()

    node.create_subscription(String, "/produce_orders/workcell_status", status,
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    node.create_subscription(String, "/produce_orders/events",
        lambda msg: events.append(json.loads(msg.data)), 100)
    client = node.create_client(SubmitOrder, "/produce_orders/submit")
    outcomes = []
    batch = datetime.now().strftime("logic-%Y%m%d-%H%M%S")
    try:
        deadline = time.monotonic() + args.ready_timeout
        last = None
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.2)
            current = state[0].get("state")
            if current != last:
                print("WORKCELL " + str(current) + ": " + state[0].get("reason", ""), flush=True)
                last = current
            if current == "READY" and client.service_is_ready() and time.monotonic() - updated[0] < 3:
                break
            if current in ("RECOVERY_REQUIRED", "INVENTORY_ERROR"):
                raise RuntimeError("Workcell requires maintenance; no order was submitted")
        else:
            raise TimeoutError("Workcell readiness timed out; no order was submitted")
        active_jobs = [j for j in state[0].get("orders", [])
                       if j.get("state") not in ("FAILED", "CANCELLED", "CANCELED", "COLLECTED")]
        if any(b.get("state") != "FREE" for b in state[0].get("bins", [])) or active_jobs:
            raise RuntimeError("Existing business resources present; refusing to overwrite them")
        for number, lines in enumerate(demands, start=1):
            identifier = f"{batch}-{number}"
            started = time.monotonic()
            response = client.call_async(SubmitOrder.Request(
                order_id=identifier, zone="A", lines=lines, priority=0))
            rclpy.spin_until_future_complete(node, response, timeout_sec=12)
            if not response.done() or response.exception():
                raise RuntimeError("Registration result unknown; inspect queue, do not duplicate request")
            receipt = response.result()
            if not receipt.success:
                raise RuntimeError("Registration rejected: " + receipt.message)
            print("REGISTERED " + identifier, flush=True)
            last = None
            deadline = time.monotonic() + args.order_timeout
            while time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=.2)
                job = next((j for j in state[0].get("orders", []) if j["order_id"] == identifier), None)
                if not job:
                    continue
                progress = (job["state"], job["completed"], job.get("reason"))
                if progress != last:
                    print(identifier + " " + json.dumps(job, ensure_ascii=False), flush=True)
                    last = progress
                if job["state"] in ("WAITING_PICKUP", "FAILED", "CANCELLED", "CANCELED") or job.get("review_required"):
                    selected_events = [e for e in events if e.get("order_id") == identifier]
                    entry = dict(order_id=identifier, demand=[dict(sku=l.sku, quantity=l.quantity) for l in lines],
                        elapsed_s=round(time.monotonic() - started, 2), job=job,
                        perception_calls=sum(e.get("stage") == "PERCEPTION_PIPELINE" for e in selected_events),
                        candidate_rejections=[e for e in selected_events if e.get("stage") == "CANDIDATE_REJECTED"],
                        empty_attempts=sum(e.get("stage") == "PERCEPTION_EMPTY" for e in selected_events))
                    outcomes.append(entry)
                    print("RESULT " + json.dumps(entry, ensure_ascii=False), flush=True)
                    break
            else:
                outcomes.append(dict(order_id=identifier, status="TIMEOUT_OR_WAITING", workcell=state[0]))
                print("BATCH STOP: order still waiting/running; no automatic reset or duplicate", flush=True)
                break
            if job["state"] != "WAITING_PICKUP":
                break
    finally:
        directory = Path.home() / ".local/share/produce_order_workstation/reports"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch}-batch.json"
        path.write_text(json.dumps(dict(outcomes=outcomes, latest_workcell=state[0]),
                                   ensure_ascii=False, indent=2) + "\n")
        print("REPORT " + str(path), flush=True)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
