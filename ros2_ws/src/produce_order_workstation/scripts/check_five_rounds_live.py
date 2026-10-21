#!/usr/bin/env python3
"""Legacy five-single-order preflight, NOT five four-bin rounds.

Retained for reproducing the preliminary failures documented in
docs/FIVE_ROUND_LIVE_STRESS_2026-10-21.md. The user's corrected acceptance
requires twenty orders, retaining all four bins per round, and resuming
paused orders after replenishment rather than discarding them.
SQLite is read-only in this probe.
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
from produce_order_interfaces.msg import OrderLine
from produce_order_interfaces.srv import CancelQueuedOrder, ConfirmPickup, SubmitOrder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--collect-existing", default="")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--prefix", default="")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--discard-failed-test-orders", action="store_true")
    args = parser.parse_args()
    if not args.database.is_file() or not 1 <= args.rounds <= 5:
        parser.error("An existing database and 1..5 rounds are required")
    rclpy.init()
    node = rclpy.create_node("five_round_live_probe")
    status = {}
    started = time.monotonic()
    prefix = args.prefix or datetime.now().strftime("stress-five-%Y%m%d-%H%M%S")
    skus = ("corn", "carrot", "apple", "tomato")
    quantities = ((1, 1, 1, 1), (2, 1, 1, 2), (1, 2, 2, 1), (2, 2, 1, 1), (2, 2, 2, 2))
    args.log.parent.mkdir(parents=True, exist_ok=True)

    def emit(event, **body):
        line = json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round(time.monotonic() - started, 2),
                "event": event,
                **body,
            },
            ensure_ascii=False,
        )
        with args.log.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
        print(line, flush=True)

    def receive(msg):
        status.clear()
        status.update(json.loads(msg.data))

    subscription = node.create_subscription(
        String,
        "/produce_orders/workcell_status",
        receive,
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
    )

    def wait(predicate, timeout=180):
        end = time.monotonic() + timeout
        last_report = 0
        while not predicate():
            if time.monotonic() >= end:
                raise RuntimeError("Timeout: " + json.dumps(status, ensure_ascii=False))
            rclpy.spin_once(node, timeout_sec=0.2)
            if time.monotonic() - last_report > 30:
                emit(
                    "WAIT",
                    state=status.get("state"),
                    reason=status.get("reason"),
                    orders=[
                        {
                            k: row.get(k)
                            for k in (
                                "order_id",
                                "state",
                                "completed",
                                "total",
                                "reason",
                            )
                        }
                        for row in status.get("orders", [])
                        if row["order_id"].startswith(prefix)
                    ],
                )
                last_report = time.monotonic()

    def call(kind, name, request):
        client = node.create_client(kind, name)
        try:
            if not client.wait_for_service(timeout_sec=5):
                raise RuntimeError(name + " unavailable")
            future = client.call_async(request)
            wait(future.done, 30)
            response = future.result()
            emit(
                "SERVICE", name=name, success=response.success, message=response.message
            )
            if not response.success:
                raise RuntimeError(response.message)
            return response
        finally:
            node.destroy_client(client)

    def rows(query, values=()):
        with sqlite3.connect(
            args.database.resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            return db.execute(query, values).fetchall()

    def pickup(identifier):
        call(
            ConfirmPickup,
            "/produce_orders/confirm_pickup",
            ConfirmPickup.Request(order_id=identifier),
        )
        assert rows("SELECT state FROM orders WHERE order_id=?", (identifier,)) == [
            ("COLLECTED",)
        ]
        assert not rows(
            "SELECT bin_id FROM bin_allocations WHERE order_id=? AND released IS NULL",
            (identifier,),
        )
        emit("PICKUP_VERIFIED", order_id=identifier)

    def refresh(replenish=False):
        name = "/produce_orders/" + ("replenish" if replenish else "refresh_inventory")
        call(Trigger, name, Trigger.Request())
        # Observe the asynchronous work, not the stale pre-request READY message.
        wait(
            lambda: status.get("state")
            in {"SCANNING", "REPLENISHING", "INVENTORY_ERROR", "RECOVERY_REQUIRED"},
            30,
        )
        wait(lambda: status.get("state") not in {"SCANNING", "REPLENISHING"}, 300)
        if status.get("state") != "READY":
            raise RuntimeError("Inventory refresh failed: " + str(status.get("reason")))
        emit(
            "INVENTORY_VERIFIED",
            replenished=replenish,
            inventory=status.get("inventory_summary"),
            available=rows(
                "SELECT sku,count(*) FROM inventory_items WHERE state='AVAILABLE' GROUP BY sku"
            ),
        )

    try:
        wait(lambda: bool(status), 30)
        emit("START", database=str(args.database), status=status, rounds=args.rounds)
        if args.collect_existing:
            assert rows(
                "SELECT state FROM orders WHERE order_id=?", (args.collect_existing,)
            ) == [("WAITING_PICKUP",)]
            pickup(args.collect_existing)
        # A restarted monitor may attach to an already-running order without
        # refreshing the scene, resubmitting it, or canceling robot execution.
        if not rows(
            "SELECT order_id FROM orders WHERE order_id LIKE ?", (prefix + "%",)
        ):
            refresh()
        results = []
        for number in range(1, args.rounds + 1):
            identifier = f"{prefix}-r{number}"
            demand = dict(zip(skus, quantities[number - 1]))
            previous = rows("SELECT state FROM orders WHERE order_id=?", (identifier,))
            if previous == [("COLLECTED",)]:
                emit("ALREADY_COLLECTED", round=number, order_id=identifier)
                results.append({"round": number, "success": True})
                continue
            if previous and previous[0][0] in {"CANCELED", "CANCELLED"}:
                emit("PREVIOUS_FAILED_ROUND", round=number, order_id=identifier)
                results.append({"round": number, "success": False})
                continue
            if previous == [("FAILED",)] and not rows(
                "SELECT bin_id FROM bin_allocations WHERE order_id=? AND released IS NULL",
                (identifier,),
            ):
                assert not rows(
                    "SELECT operation_id FROM operations WHERE order_id=? AND state IN ('INTENT','UNCERTAIN')",
                    (identifier,),
                )
                emit("PREVIOUS_FAILED_ROUND", round=number, order_id=identifier)
                results.append({"round": number, "success": False})
                continue
            if not previous:
                available = dict(
                    rows(
                        "SELECT sku,count(*) FROM inventory_items WHERE state='AVAILABLE' GROUP BY sku"
                    )
                )
                if any(available.get(sku, 0) < count for sku, count in demand.items()):
                    refresh(replenish=True)
                wait(lambda: status.get("can_submit"), 120)
                emit("ROUND_START", round=number, order_id=identifier, demand=demand)
                request = SubmitOrder.Request(
                    order_id=identifier,
                    zone="A",
                    priority=0,
                    lines=[
                        OrderLine(sku=sku, quantity=count)
                        for sku, count in demand.items()
                    ],
                )
                call(SubmitOrder, "/produce_orders/submit", request)
            else:
                emit(
                    "ATTACHED_EXISTING",
                    round=number,
                    order_id=identifier,
                    state=previous[0][0],
                )
            terminal = {
                "WAITING_PICKUP",
                "FAILED",
                "CANCELED",
                "CANCELLED",
                "RECOVERY_REQUIRED",
                "EXECUTION_UNCERTAIN",
                "PAUSED",
                "WAITING_STOCK",
            }
            wait(
                lambda: any(
                    row["order_id"] == identifier and row["state"] in terminal
                    for row in status.get("orders", [])
                ),
                900,
            )
            job = next(row for row in status["orders"] if row["order_id"] == identifier)
            emit("ROUND_RESULT", round=number, order=job)
            if job["state"] != "WAITING_PICKUP":
                results.append({"round": number, "success": False, "order": job})
                if not args.discard_failed_test_orders or job["state"] in {
                    "RECOVERY_REQUIRED",
                    "EXECUTION_UNCERTAIN",
                }:
                    raise RuntimeError("Order failed; preserving scene: " + str(job))
                # Only this explicitly named probe order can be cleaned up;
                # unknown physical results stop the whole test above.
                wait(
                    lambda: status.get("state")
                    in {"READY", "INVENTORY_ERROR", "RECOVERY_REQUIRED"},
                    180,
                )
                if status.get("state") == "RECOVERY_REQUIRED":
                    raise RuntimeError("Unknown physical state; no automatic cleanup")
                allocation = rows(
                    "SELECT state FROM bin_allocations WHERE order_id=? AND released IS NULL",
                    (identifier,),
                )
                if allocation or job["state"] != "FAILED":
                    call(
                        CancelQueuedOrder,
                        "/produce_orders/cancel_queued",
                        CancelQueuedOrder.Request(
                            order_id=identifier, discard_partial=bool(allocation)
                        ),
                    )
                emit(
                    "FAILED_TEST_CLEANED",
                    round=number,
                    order_id=identifier,
                    placed=rows(
                        "SELECT sku,count(*) FROM placed_items WHERE order_id=? GROUP BY sku",
                        (identifier,),
                    ),
                )
                refresh()
                if number < args.rounds:
                    refresh(replenish=True)
                continue
            progress = rows(
                "SELECT sku,quantity,completed FROM order_progress WHERE order_id=? ORDER BY sku",
                (identifier,),
            )
            assert progress == [(sku, demand[sku], demand[sku]) for sku in sorted(skus)]
            for query in (
                "SELECT count(*) FROM placed_items WHERE order_id=?",
                "SELECT count(*) FROM reservation_status WHERE order_id=? AND state='CONSUMED'",
                "SELECT count(*) FROM operations WHERE order_id=? AND phase='PICK_AND_PLACE' AND state='VERIFIED'",
            ):
                assert rows(query, (identifier,)) == [(sum(demand.values()),)]
            assert rows(
                "SELECT count(*) FROM orders WHERE order_id=?", (identifier,)
            ) == [(1,)]
            assert rows("PRAGMA integrity_check") == [("ok",)]
            assert not rows("PRAGMA foreign_key_check")
            emit("SQL_VERIFIED", round=number, progress=progress)
            pickup(identifier)
            refresh()
            emit("ROUND_COMPLETE", round=number, order_id=identifier)
            results.append({"round": number, "success": True})
        emit(
            "ALL_ATTEMPTS_COMPLETE",
            rounds=args.rounds,
            planned_picks=sum(sum(q) for q in quantities[: args.rounds]),
            success_rounds=sum(result["success"] for result in results),
            results=results,
        )
    except Exception as exc:
        emit("FAILED", error=str(exc))
        raise
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
