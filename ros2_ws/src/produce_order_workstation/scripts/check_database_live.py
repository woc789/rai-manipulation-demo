#!/usr/bin/env python3
"""One real order via SubmitOrder; inspect SQLite evidence without writing it.

Requires an already-running full simulator. Uses actual vision, model, MCP,
behavior tree and MoveIt. Never creates stock or resets the scene.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from produce_order_interfaces.msg import OrderLine
from produce_order_interfaces.srv import ConfirmPickup, SubmitOrder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--skus",
        nargs="+",
        default=["carrot"],
        choices=["corn", "carrot", "apple", "tomato"],
    )
    parser.add_argument("--confirm-pickup", action="store_true")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("Database must already exist; this probe never creates one")
    rclpy.init()
    node = rclpy.create_node("database_live_probe")
    status = {}

    def receive(msg):
        status.clear()
        status.update(json.loads(msg.data))

    node.create_subscription(
        String,
        "/produce_orders/workcell_status",
        receive,
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
    )

    def wait(predicate, seconds):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "Timed out; inspect workcell, no reset/replay performed"
                )
            rclpy.spin_once(node, timeout_sec=0.2)

    def call(kind, name, request):
        client = node.create_client(kind, name)
        try:
            if not client.wait_for_service(timeout_sec=5):
                raise RuntimeError(name + " unavailable")
            future = client.call_async(request)
            wait(future.done, 15)
            response = future.result()
            if not response.success:
                raise RuntimeError(response.message)
            print(response, flush=True)
            return response
        finally:
            node.destroy_client(client)

    try:
        wait(lambda: status.get("can_submit"), 180)
        identifier = datetime.now().strftime("database-live-%Y%m%d-%H%M%S-%f")
        print("ORDER " + identifier, flush=True)
        request = SubmitOrder.Request(
            order_id=identifier,
            zone="A",
            priority=0,
            lines=[
                OrderLine(sku=sku, quantity=args.skus.count(sku))
                for sku in dict.fromkeys(args.skus)
            ],
        )
        call(SubmitOrder, "/produce_orders/submit", request)
        # Public idempotence check, not a second order.
        call(SubmitOrder, "/produce_orders/submit", request)
        terminal = {
            "WAITING_PICKUP",
            "FAILED",
            "CANCELLED",
            "CANCELED",
            "RECOVERY_REQUIRED",
            "EXECUTION_UNCERTAIN",
        }
        wait(
            lambda: any(
                row["order_id"] == identifier and row["state"] in terminal
                for row in status.get("orders", [])
            ),
            480,
        )
        row = next(row for row in status["orders"] if row["order_id"] == identifier)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if row["state"] != "WAITING_PICKUP":
            raise RuntimeError("Real order did not complete: " + row["reason"])
        with sqlite3.connect(
            args.database.resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            count = db.execute(
                "SELECT count(*) FROM orders WHERE order_id=?", (identifier,)
            ).fetchone()[0]
            completed = db.execute(
                "SELECT sum(completed) FROM order_lines WHERE order_id=?", (identifier,)
            ).fetchone()[0]
            placed = db.execute(
                "SELECT count(*) FROM placed_items WHERE order_id=?", (identifier,)
            ).fetchone()[0]
            consumed = db.execute(
                "SELECT count(*) FROM reservations WHERE order_id=? AND state='CONSUMED'",
                (identifier,),
            ).fetchone()[0]
            verified = db.execute(
                "SELECT count(*) FROM operations WHERE order_id=? AND phase='PICK_AND_PLACE' AND state='VERIFIED'",
                (identifier,),
            ).fetchone()[0]
            assert count == 1 and completed == placed == consumed == verified == len(
                args.skus
            )
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
        print(
            "SQL VERIFIED: order/lines/placement/reservation/operation agree",
            flush=True,
        )
        if args.confirm_pickup:
            call(
                ConfirmPickup,
                "/produce_orders/confirm_pickup",
                ConfirmPickup.Request(order_id=identifier),
            )
            with sqlite3.connect(
                args.database.resolve().as_uri() + "?mode=ro", uri=True
            ) as db:
                assert (
                    db.execute(
                        "SELECT state FROM orders WHERE order_id=?", (identifier,)
                    ).fetchone()[0]
                    == "COLLECTED"
                )
                assert (
                    db.execute(
                        "SELECT count(*) FROM bin_allocations WHERE order_id=? AND released IS NULL",
                        (identifier,),
                    ).fetchone()[0]
                    == 0
                )
            print("SQL VERIFIED: collected and bin released", flush=True)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
