#!/usr/bin/env python3
"""Real five-cycle test: four orders/retained bins per cycle, then pickup.

Uses only public ROS services for mutations; SQLite evidence is read-only.
Never discards, replaces, or directly edits a failed/paused order.
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
from produce_order_interfaces.srv import ConfirmPickup, SubmitOrder


SKUS = ("corn", "carrot", "apple", "tomato")
PLAN = (
    ((2, 1, 0, 1), (0, 2, 1, 2), (1, 0, 2, 1), (2, 1, 1, 2)),
    ((1, 2, 1, 0), (2, 0, 1, 2), (0, 1, 2, 2), (2, 2, 1, 1)),
    ((2, 1, 2, 1), (1, 2, 0, 2), (0, 1, 2, 1), (2, 0, 1, 2)),
    ((1, 1, 2, 0), (2, 1, 1, 2), (1, 2, 1, 1), (2, 2, 2, 2)),
    ((2, 1, 1, 1), (1, 2, 2, 1), (2, 2, 1, 2), (2, 2, 2, 2)),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--collect-existing", default="")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("The active database must already exist")
    args.log.parent.mkdir(parents=True, exist_ok=True)
    rclpy.init()
    node = rclpy.create_node("four_bin_five_cycle_probe")
    status = {}

    def emit(event, **body):
        line = json.dumps(
            {"time": datetime.now(timezone.utc).isoformat(), "event": event, **body},
            ensure_ascii=False,
        )
        with args.log.open("a", encoding="utf-8") as output:
            output.write(line + "\n")
        print(line, flush=True)

    def receive(message):
        status.clear()
        status.update(json.loads(message.data))

    subscription = node.create_subscription(
        String,
        "/produce_orders/workcell_status",
        receive,
        QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
    )

    def rows(sql, params=()):
        with sqlite3.connect(
            args.database.resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            return db.execute(sql, params).fetchall()

    def spin(seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=min(0.2, end - time.monotonic()))

    def wait(predicate, seconds):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(str(status.get("reason")))
            spin()

    def call(kind, name, request, *, retry_busy=False):
        client = node.create_client(kind, name)
        try:
            if not client.wait_for_service(timeout_sec=5):
                raise RuntimeError(name + " unavailable")
            for attempt in range(10 if retry_busy else 1):
                future = client.call_async(request)
                wait(future.done, 30)
                response = future.result()
                emit(
                    "SERVICE",
                    name=name,
                    success=response.success,
                    message=response.message,
                )
                if response.success:
                    return response
                busy = "忙" in response.message or "只有现场就绪" in response.message
                if not retry_busy or not busy or attempt == 9:
                    raise RuntimeError(response.message)
                spin(2)
        finally:
            node.destroy_client(client)

    def refresh(replenish=False):
        name = "/produce_orders/" + ("replenish" if replenish else "refresh_inventory")
        before_epoch = int(
            rows("SELECT value FROM metadata WHERE key='scene_epoch'")[0][0]
        )
        call(Trigger, name, Trigger.Request(), retry_busy=True)
        wait(
            lambda: status.get("state")
            in {"SCANNING", "REPLENISHING", "INVENTORY_ERROR", "RECOVERY_REQUIRED"},
            30,
        )
        wait(lambda: status.get("state") not in {"SCANNING", "REPLENISHING"}, 300)
        if status.get("state") in {
            "INVENTORY_ERROR",
            "RECOVERY_REQUIRED",
            "SCENE_OFFLINE",
            "DATABASE_ERROR",
        }:
            raise RuntimeError(str(status.get("reason")))
        epoch = int(rows("SELECT value FROM metadata WHERE key='scene_epoch'")[0][0])
        if replenish:
            assert epoch == before_epoch + 1
        emit(
            "REPLENISH_DONE" if replenish else "REFRESH_DONE",
            epoch=epoch,
            inventory=status.get("inventory_summary"),
        )

    def pickup(identifier):
        assert rows("SELECT state FROM orders WHERE order_id=?", (identifier,)) == [
            ("WAITING_PICKUP",)
        ]
        call(
            ConfirmPickup,
            "/produce_orders/confirm_pickup",
            ConfirmPickup.Request(order_id=identifier),
            retry_busy=True,
        )
        assert rows("SELECT state FROM orders WHERE order_id=?", (identifier,)) == [
            ("COLLECTED",)
        ]
        assert not rows(
            "SELECT bin_id FROM bin_allocations WHERE order_id=? AND released IS NULL",
            (identifier,),
        )
        emit("PICKUP_VERIFIED", order_id=identifier)

    def verify_order(identifier, quantities):
        expected = [
            (sku, count, count) for sku, count in sorted(zip(SKUS, quantities)) if count
        ]
        assert (
            rows(
                "SELECT sku,quantity,completed FROM order_progress WHERE order_id=? ORDER BY sku",
                (identifier,),
            )
            == expected
        )
        for sql in (
            "SELECT count(*) FROM placed_items WHERE order_id=?",
            "SELECT count(*) FROM reservation_status WHERE order_id=? AND state='CONSUMED'",
            "SELECT count(*) FROM operations WHERE order_id=? AND phase='PICK_AND_PLACE' AND state='VERIFIED'",
        ):
            assert rows(sql, (identifier,)) == [(sum(quantities),)]
        assert rows("SELECT count(*) FROM orders WHERE order_id=?", (identifier,)) == [
            (1,)
        ]

    try:
        wait(lambda: bool(status), 30)
        emit("START", prefix=args.prefix, plan=PLAN, database=str(args.database))
        if args.collect_existing:
            wait(lambda: status.get("can_pickup"), 180)
            pickup(args.collect_existing)
        refresh()
        completed_cycles = 0
        for cycle, orders in enumerate(PLAN, 1):
            identifiers = [f"{args.prefix}-r{cycle}-o{index}" for index in range(1, 5)]
            emit("CYCLE_START", cycle=cycle, orders=dict(zip(identifiers, orders)))
            for identifier, quantities in zip(identifiers, orders):
                if not rows(
                    "SELECT order_id FROM orders WHERE order_id=?", (identifier,)
                ):
                    call(
                        SubmitOrder,
                        "/produce_orders/submit",
                        SubmitOrder.Request(
                            order_id=identifier,
                            zone="A",
                            priority=0,
                            lines=[
                                OrderLine(sku=sku, quantity=count)
                                for sku, count in zip(SKUS, quantities)
                                if count
                            ],
                        ),
                    )
            deadline = time.monotonic() + 2400
            last_report, idle_since, previous_progress = 0, None, -1
            refills_without_progress, refreshes_without_progress = 0, 0
            while True:
                spin()
                jobs = {
                    row["order_id"]: row
                    for row in status.get("orders", [])
                    if row["order_id"] in identifiers
                }
                if len(jobs) != 4:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Not all four orders became visible")
                    continue
                if time.monotonic() - last_report >= 30:
                    emit(
                        "PROGRESS",
                        cycle=cycle,
                        state=status.get("state"),
                        jobs=[
                            {
                                key: job.get(key)
                                for key in (
                                    "order_id",
                                    "state",
                                    "completed",
                                    "total",
                                    "reason",
                                )
                            }
                            for job in jobs.values()
                        ],
                    )
                    last_report = time.monotonic()
                failed = [
                    job
                    for job in jobs.values()
                    if job["state"]
                    in {
                        "FAILED",
                        "CANCELED",
                        "CANCELLED",
                        "RECOVERY_REQUIRED",
                        "EXECUTION_UNCERTAIN",
                    }
                ]
                if failed:
                    raise RuntimeError(
                        "Round cannot complete; original orders retained: "
                        + str(failed)
                    )
                if all(job["state"] == "WAITING_PICKUP" for job in jobs.values()):
                    break
                if status.get("state") in {
                    "SCENE_OFFLINE",
                    "DATABASE_ERROR",
                    "RECOVERY_REQUIRED",
                }:
                    raise RuntimeError(str(status.get("reason")))
                progress = sum(job["completed"] for job in jobs.values())
                if progress != previous_progress:
                    previous_progress = progress
                    refills_without_progress = refreshes_without_progress = 0
                active = any(
                    job["state"] in {"RUNNING", "DISPATCHING"} for job in jobs.values()
                )
                idle = (
                    not active
                    and status.get("can_pickup")
                    and status.get("state") == "READY"
                )
                idle_since = (idle_since or time.monotonic()) if idle else None
                if idle_since and time.monotonic() - idle_since > 3:
                    if refreshes_without_progress == 0:
                        emit("RESUME_REVIEW", cycle=cycle, completed=progress)
                        refresh()
                        refreshes_without_progress += 1
                    elif refills_without_progress < 2:
                        # Source replacement must not touch any retained box or placement.
                        before = rows("SELECT * FROM placed_items ORDER BY item_id")
                        bindings = rows(
                            "SELECT * FROM bin_allocations WHERE released IS NULL ORDER BY bin_id"
                        )
                        emit("REPLENISH_FOR_PENDING", cycle=cycle, completed=progress)
                        refresh(replenish=True)
                        assert (
                            rows("SELECT * FROM placed_items ORDER BY item_id")
                            == before
                        )
                        # Allocation state may legitimately advance on admission;
                        # owner, bin, creation and unreleased identity must persist.
                        after = rows(
                            "SELECT order_id,bin_id,created FROM bin_allocations WHERE released IS NULL ORDER BY bin_id"
                        )
                        assert all(
                            (row[0], row[1], row[3]) in after for row in bindings
                        )
                        refills_without_progress += 1
                    else:
                        raise RuntimeError(
                            "No progress after two replenishments; preserve paused orders and all boxes"
                        )
                    idle_since = None
                if time.monotonic() >= deadline:
                    raise TimeoutError("Cycle timed out; no replay/reset performed")
            allocations = rows(
                "SELECT order_id,bin_id FROM bin_allocations WHERE released IS NULL"
            )
            assert set(row[0] for row in allocations) == set(identifiers)
            assert len(set(row[1] for row in allocations)) == 4
            for identifier, quantities in zip(identifiers, orders):
                verify_order(identifier, quantities)
            assert rows("PRAGMA integrity_check") == [("ok",)]
            assert not rows("PRAGMA foreign_key_check")
            emit("FOUR_BINS_VERIFIED", cycle=cycle, allocations=allocations)
            # No pickup occurs until all four original orders are complete.
            for identifier in identifiers:
                pickup(identifier)
            refresh()
            completed_cycles += 1
            emit("CYCLE_COMPLETE", cycle=cycle)
        emit(
            "ALL_COMPLETE",
            cycles=completed_cycles,
            orders=completed_cycles * 4,
            pieces=sum(sum(q) for cycle in PLAN for q in cycle),
        )
    except Exception as exc:
        emit(
            "BLOCKED",
            error=str(exc),
            state=status.get("state"),
            orders=[
                row
                for row in status.get("orders", [])
                if row["order_id"].startswith(args.prefix)
            ],
        )
        raise
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
