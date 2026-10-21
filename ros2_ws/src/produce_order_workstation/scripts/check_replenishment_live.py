#!/usr/bin/env python3
"""Watch an existing order finish, then replenish without collecting its box.

No order submission, reset, pickup, or direct database writes are performed.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from std_srvs.srv import Trigger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--order", required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("The live database must already exist")
    rclpy.init()
    node = rclpy.create_node("replenishment_resume_live_probe")
    status = {}

    def emit(event, **details):
        print(
            json.dumps(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "event": event,
                    **details,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def receive(message):
        status.clear()
        status.update(json.loads(message.data))

    subscription = node.create_subscription(
        String,
        "/produce_orders/workcell_status",
        receive,
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
    )

    def rows(sql, values=()):
        with sqlite3.connect(
            args.database.resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            return db.execute(sql, values).fetchall()

    def wait(predicate, seconds):
        deadline, last = time.monotonic() + seconds, 0
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(json.dumps(status, ensure_ascii=False))
            rclpy.spin_once(node, timeout_sec=0.2)
            if time.monotonic() - last > 30:
                emit(
                    "WAIT",
                    state=status.get("state"),
                    reason=status.get("reason"),
                    orders=[
                        row
                        for row in status.get("orders", [])
                        if row["order_id"] == args.order
                    ],
                )
                last = time.monotonic()

    try:
        wait(lambda: bool(status), 30)
        emit(
            "BASELINE",
            order=args.order,
            progress=rows(
                "SELECT sku,quantity,completed FROM order_progress WHERE order_id=? ORDER BY sku",
                (args.order,),
            ),
        )
        terminal = {"WAITING_PICKUP", "FAILED", "EXECUTION_UNCERTAIN"}
        wait(
            lambda: any(
                row["order_id"] == args.order
                and (
                    row["state"] in terminal
                    or (row["state"] == "PAUSED" and row["review_required"])
                )
                for row in status.get("orders", [])
            ),
            600,
        )
        job = next(row for row in status["orders"] if row["order_id"] == args.order)
        emit("ORDER_RESULT", order=job)
        if job["state"] != "WAITING_PICKUP":
            raise RuntimeError("Order not complete; preserving its scene")
        wait(lambda: status.get("state") == "READY" and status.get("can_pickup"), 180)
        progress = rows(
            "SELECT sku,quantity,completed FROM order_progress WHERE order_id=? ORDER BY sku",
            (args.order,),
        )
        assert all(quantity == completed for _, quantity, completed in progress)
        placements = rows(
            "SELECT * FROM placed_items WHERE order_id=? ORDER BY item_id",
            (args.order,),
        )
        bins = rows(
            "SELECT * FROM bin_allocations WHERE order_id=? AND released IS NULL",
            (args.order,),
        )
        assert bins
        epoch = rows("SELECT value FROM metadata WHERE key='scene_epoch'")
        client = node.create_client(Trigger, "/produce_orders/replenish")
        try:
            if not client.wait_for_service(timeout_sec=5):
                raise RuntimeError("Replenishment service unavailable")
            future = client.call_async(Trigger.Request())
            wait(future.done, 30)
            response = future.result()
            emit(
                "REPLENISH_REQUEST", success=response.success, message=response.message
            )
            if not response.success:
                raise RuntimeError(response.message)
            wait(
                lambda: status.get("state")
                in {"REPLENISHING", "SCANNING", "INVENTORY_ERROR", "RECOVERY_REQUIRED"},
                30,
            )
            wait(lambda: status.get("state") not in {"REPLENISHING", "SCANNING"}, 300)
        finally:
            node.destroy_client(client)
        emit(
            "REPLENISH_RESULT",
            state=status.get("state"),
            reason=status.get("reason"),
            inventory=status.get("inventory_summary"),
            initial_check=status.get("initial_inventory_check"),
        )
        assert status.get("state") == "READY", "Replenishment did not become ready"
        assert status.get("initial_inventory_check") is None
        assert (
            rows(
                "SELECT * FROM placed_items WHERE order_id=? ORDER BY item_id",
                (args.order,),
            )
            == placements
        )
        assert (
            rows(
                "SELECT * FROM bin_allocations WHERE order_id=? AND released IS NULL",
                (args.order,),
            )
            == bins
        )
        assert rows("SELECT state FROM orders WHERE order_id=?", (args.order,)) == [
            ("WAITING_PICKUP",)
        ]
        assert (
            int(rows("SELECT value FROM metadata WHERE key='scene_epoch'")[0][0])
            == int(epoch[0][0]) + 1
        )
        assert rows("PRAGMA integrity_check") == [("ok",)]
        assert not rows("PRAGMA foreign_key_check")
        emit(
            "VERIFIED",
            progress=progress,
            bin_retained=True,
            placements_unchanged=True,
            source_epoch_advanced=True,
        )
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
