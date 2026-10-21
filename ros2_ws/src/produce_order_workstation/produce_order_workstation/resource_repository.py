"""Orders and receiving resources queried from SQLite, with no mutable state containers."""

from dataclasses import dataclass
import json
import time
from types import SimpleNamespace

from .order import Order, OrderLine, fulfillment_mode


STATE_LABELS = {
    "QUEUED": "排队",
    "WAITING_STOCK": "等待有效库存观测或补货",
    "WAITING_BIN": "等待空闲箱位",
    "DISPATCHING": "正在派发",
    "RUNNING": "执行中",
    "PAUSED": "暂停",
    "WAITING_PICKUP": "配货完成，尚未取货",
    "COLLECTED": "已经确认取货，箱位已释放",
    "RECOVERY_REQUIRED": "需要现场核对",
    "FAILED": "失败",
    "CANCELED": "已取消",
    "CANCELLED": "已取消",
    "EXECUTION_UNCERTAIN": "执行结果未知，需核对",
}


@dataclass(frozen=True)
class OrderRecord:
    order: Order
    priority: int
    sequence: int
    state: str
    reason: str
    missing: dict
    completed: int
    total: int
    finished_sequence: int
    review_required: bool
    version: int


@dataclass(frozen=True)
class AllocationRecord:
    order: Order
    bin_indices: tuple[int, ...]
    state: str
    entities: tuple


class ResourceRepository:
    def __init__(self, store, config, *, max_pending=100):
        self.store, self.config, self.max_pending = store, config, max_pending
        with store.transaction() as db:
            for zone, data in config["zones"].items():
                for index in range(len(data["slots"])):
                    db.execute(
                        "INSERT OR IGNORE INTO bins VALUES(?,?,?)",
                        (f"{zone}{index + 1}", zone, index),
                    )

    def get(self, identifier):
        with self.store.read_transaction():
            return self._get_record(identifier)

    def _get_record(self, identifier):
        rows = self.store.rows("SELECT * FROM orders WHERE order_id=?", (identifier,))
        if not rows:
            return None
        row = rows[0]
        lines = self.store.rows(
            "SELECT p.sku,p.quantity,p.completed FROM order_progress p JOIN order_lines l USING(order_id,sku) WHERE p.order_id=? ORDER BY l.rowid",
            (identifier,),
        )
        order = Order(
            identifier,
            row["zone"],
            tuple(OrderLine(line["sku"], line["quantity"]) for line in lines),
        )
        return OrderRecord(
            order,
            row["priority"],
            row["sequence"],
            row["state"],
            row["reason"],
            json.loads(row["missing"]),
            sum(line["completed"] for line in lines) + row["unattributed_completed"],
            order.total_items,
            row["finished_sequence"],
            bool(row["review_required"]),
            row["version"],
        )

    def all(self):
        with self.store.read_transaction():
            return [
                self.get(row["order_id"])
                for row in self.store.rows(
                    "SELECT order_id FROM orders ORDER BY sequence"
                )
            ]

    def progress(self, identifier):
        return {
            row["sku"]: row["completed"]
            for row in self.store.rows(
                "SELECT sku,completed FROM order_progress WHERE order_id=?",
                (identifier,),
            )
        }

    def submit(self, order, priority=0):
        if type(priority) is not int or not 0 <= priority <= 10:
            raise ValueError("优先级必须为 0～10")
        with self.store.transaction() as db:
            existing = self.get(order.order_id)
            if existing:
                if existing.order != order or existing.priority != priority:
                    raise ValueError("订单号已有不同需求或优先级，不能覆盖")
                return existing
            if len(self.pending()) >= self.max_pending:
                raise ValueError("等待队列已满")
            now = time.time()
            db.execute(
                "INSERT INTO orders(order_id,zone,priority,created,updated) VALUES(?,?,?,?,?)",
                (order.order_id, order.zone, priority, now, now),
            )
            db.executemany(
                "INSERT INTO order_lines(order_id,sku,quantity) VALUES(?,?,?)",
                [(order.order_id, line.sku, line.quantity) for line in order.lines],
            )
            return self.get(order.order_id)

    def update(self, identifier, *, expected_version=None, **fields):
        allowed = {"state", "reason", "missing", "review_required", "finished_sequence"}
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported order update")
        values = {
            key: json.dumps(value, ensure_ascii=False) if key == "missing" else value
            for key, value in fields.items()
        }
        with self.store.transaction() as db:
            current = self.get(identifier)
            if (
                current
                and (expected_version is None or current.version == expected_version)
                and all(getattr(current, key) == value for key, value in fields.items())
            ):
                return current
            condition = "order_id=?" + (
                " AND version=?" if expected_version is not None else ""
            )
            params = [*values.values(), time.time(), identifier] + (
                [expected_version] if expected_version is not None else []
            )
            changed = db.execute(
                "UPDATE orders SET "
                + ",".join(f"{key}=?" for key in values)
                + ",updated=?,version=version+1 WHERE "
                + condition,
                params,
            ).rowcount
            if changed != 1:
                raise ValueError("Order missing or changed; refresh before retrying")
            return self.get(identifier)

    def replace_order(self, order, expected_version):
        with self.store.transaction() as db:
            job = self.get(order.order_id)
            if (
                not job
                or job.completed
                or job.state not in ("QUEUED", "WAITING_STOCK", "WAITING_BIN")
                or job.version != expected_version
            ):
                raise ValueError("Order changed or started; create a new proposal")
            db.execute("DELETE FROM order_lines WHERE order_id=?", (order.order_id,))
            db.executemany(
                "INSERT INTO order_lines(order_id,sku,quantity) VALUES(?,?,?)",
                [(order.order_id, line.sku, line.quantity) for line in order.lines],
            )
            return self.update(
                order.order_id,
                expected_version=expected_version,
                state="QUEUED",
                missing={},
                reason="操作员已确认修改，重新检查资源",
            )

    def pending(self):
        return [
            self.get(row["order_id"])
            for row in self.store.rows("""SELECT order_id FROM orders
            WHERE state IN ('QUEUED','WAITING_STOCK','WAITING_BIN','PAUSED') AND review_required=0
            ORDER BY priority DESC,sequence""")
        ]

    def select_next(self, inventory):
        if inventory.store is not self.store:
            raise ValueError(
                "Resource admission requires one shared database connection"
            )
        with self.store.transaction():
            for job in self.pending():
                allocation = self.allocation(job.order.order_id)
                if allocation is not None and allocation.state not in (
                    "ALLOCATED",
                    "PAUSED",
                ):
                    continue
                if allocation is None and not self.can_allocate(job.order.zone):
                    self.update(
                        job.order.order_id, state="WAITING_BIN", reason="等待空闲箱位"
                    )
                    continue
                _, missing = inventory.reserve_order(job.order)
                if missing:
                    ambiguous = set(missing) & inventory.ambiguous_skus()
                    reason = (
                        "同一位置被识别为不同商品（"
                        + "、".join(sorted(ambiguous))
                        + "）；请调整遮挡或视角并视觉复查，必要时人工核对；未确认前不抓取"
                        if ambiguous
                        else "可用视觉观测不足，等待复查或补货；不等同于确认缺货"
                    )
                    self.update(
                        job.order.order_id,
                        state="WAITING_STOCK",
                        reason=reason,
                        missing=missing,
                    )
                    continue
                self.allocate(job.order)
                return self.update(
                    job.order.order_id,
                    state="DISPATCHING",
                    missing={},
                    reason="资源已预留，正在发送执行请求",
                )
        return None

    def cancel(self, identifier, inventory):
        with self.store.transaction():
            job = self.get(identifier)
            if (
                not job
                or job.state not in ("QUEUED", "WAITING_STOCK", "WAITING_BIN")
                or job.completed
                or job.review_required
            ):
                raise ValueError("只能取消尚未开始调度的订单")
            inventory.release_order(identifier)
            self.update(identifier, state="CANCELED", reason="用户取消排队订单")

    def finish(self, identifier, status, completed, reason):
        if status not in ("COMPLETED", "PAUSED", "FAILED", "CANCELLED", "CANCELED"):
            raise ValueError("执行返回了未知业务状态，需核对现场")
        with self.store.transaction() as db:
            job = self.get(identifier)
            # ROS feedback is not durable proof of placement. Never promote it into inventory.
            if completed != job.completed:
                raise ValueError(
                    "Execution count differs from verified database progress; reconcile scene"
                )
            if status == "COMPLETED" and completed != job.total:
                raise ValueError("Cannot complete order before every item is verified")
            desired = "WAITING_PICKUP" if status == "COMPLETED" else status
            if job.state == desired and job.reason == reason:
                return job
            review = status == "PAUSED" and reason.startswith("[REVIEW_REQUIRED]")
            if review:
                reason = (
                    "已装箱进度保留；请点击视觉复查后继续，或确认丢弃部分订单。原因："
                    + reason.removeprefix("[REVIEW_REQUIRED] ")
                )
            sequence = db.execute(
                "SELECT COALESCE(MAX(finished_sequence),0)+1 FROM orders"
            ).fetchone()[0]
            return self.update(
                identifier,
                state=desired,
                reason=reason,
                review_required=review,
                finished_sequence=sequence
                if status != "PAUSED"
                else job.finished_sequence,
            )

    def snapshot_orders(self):
        return [
            dict(
                order_id=job.order.order_id,
                zone=job.order.zone,
                priority=job.priority,
                sequence=job.sequence,
                state=job.state,
                state_label=STATE_LABELS.get(job.state, job.state),
                reason=job.reason,
                missing=job.missing,
                version=job.version,
                completed=job.completed,
                review_required=job.review_required,
                total=job.total,
                finished_sequence=job.finished_sequence,
                lines=[
                    dict(sku=line.sku, quantity=line.quantity)
                    for line in job.order.lines
                ],
            )
            for job in self.all()
        ]

    def snapshot_bins(self):
        return [
            dict(
                bin_id=row["bin_id"],
                state=row["state"] or "FREE",
                order_id=row["order_id"],
            )
            for row in self.store.rows("""SELECT b.bin_id,a.state,a.order_id FROM bins b
                    LEFT JOIN bin_allocations a ON a.bin_id=b.bin_id AND a.released IS NULL ORDER BY b.zone,b.bin_index""")
        ]

    def bin(self, identifier):
        return next(
            (
                SimpleNamespace(**row)
                for row in self.snapshot_bins()
                if row["bin_id"] == identifier
            ),
            None,
        )

    def can_allocate(self, zone):
        rows = self.store.rows(
            """SELECT b.bin_id,a.order_id FROM bins b LEFT JOIN bin_allocations a
            ON a.bin_id=b.bin_id AND a.released IS NULL WHERE b.zone=?""",
            (zone,),
        )
        return bool(rows) and (
            any(row["order_id"] is None for row in rows)
            if fulfillment_mode(self.config) == "fulfillment"
            else all(row["order_id"] is None for row in rows)
        )

    def allocation(self, identifier):
        rows = self.store.rows(
            """SELECT b.bin_index,a.state FROM bin_allocations a JOIN bins b USING(bin_id)
            WHERE a.order_id=? ORDER BY b.bin_index""",
            (identifier,),
        )
        if not rows:
            return None
        return AllocationRecord(
            self.get(identifier).order,
            tuple(row["bin_index"] for row in rows),
            rows[0]["state"],
            tuple(self.placements(identifier)),
        )

    def allocate(self, order):
        with self.store.transaction() as db:
            existing = self.allocation(order.order_id)
            if existing:
                if existing.state not in ("ALLOCATED", "PAUSED"):
                    raise ValueError("Order ID already owns a bin or was collected")
                return existing
            if not self.can_allocate(order.zone):
                raise ValueError("No receiving bin is available; confirm pickup first")
            if self.get(order.order_id) is None:
                self.submit(order)
            free = [
                row["bin_id"]
                for row in self.snapshot_bins()
                if row["state"] == "FREE" and row["bin_id"].startswith(order.zone)
            ]
            names = (
                free[:1]
                if fulfillment_mode(self.config) == "fulfillment"
                else [
                    f"{order.zone}{self.config['zones'][order.zone]['sku_bins'][line.sku] + 1}"
                    for line in order.lines
                ]
            )
            now = time.time()
            for name in dict.fromkeys(names):
                db.execute(
                    "INSERT INTO bin_allocations VALUES(?,?,'ALLOCATED',?,NULL)",
                    (order.order_id, name, now),
                )
            return self.allocation(order.order_id)

    def allocation_state(self, identifier, state, *, release=False):
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE bin_allocations SET state=?,released=? WHERE order_id=? AND released IS NULL",
                (state, time.time() if release else None, identifier),
            ).rowcount
            if not changed:
                raise ValueError("No active order bin allocation")

    def finish_allocation(self, identifier, status, physical_uncertain):
        state = (
            "WAITING_PICKUP"
            if status == "COMPLETED"
            else "PAUSED"
            if status == "PAUSED"
            else "RECOVERY_REQUIRED"
            if physical_uncertain
            else status
        )
        self.allocation_state(
            identifier,
            state,
            release=state not in ("WAITING_PICKUP", "PAUSED", "RECOVERY_REQUIRED"),
        )

    def pickup(self, identifier):
        allocation = self.allocation(identifier)
        if not allocation or allocation.state != "WAITING_PICKUP":
            raise ValueError(
                "Only a completed order waiting for pickup can be collected"
            )
        return allocation

    def confirm_pickup(self, identifier, inventory):
        with self.store.transaction():
            self.pickup(identifier)
            inventory.collect(identifier)
            self.allocation_state(identifier, "COLLECTED", release=True)
            self.update(identifier, state="COLLECTED", reason="已确认取货，箱位已释放")

    def discard_paused(self, identifier, inventory):
        with self.store.transaction():
            allocation = self.allocation(identifier)
            if not allocation or allocation.state != "PAUSED":
                raise ValueError("Only a safely paused order can be discarded")
            inventory.collect(identifier)
            inventory.release_order(identifier)
            self.allocation_state(identifier, "CANCELED", release=True)
            self.update(
                identifier,
                state="CANCELED",
                reason="用户确认丢弃部分配货，已移走订单箱",
            )

    def can_reset(self):
        return not self.store.rows(
            "SELECT 1 FROM bin_allocations WHERE released IS NULL AND state IN ('WAITING_PICKUP','ALLOCATED','PAUSED')"
        )

    def reset(self):
        with self.store.transaction() as db:
            if not self.can_reset():
                raise ValueError("Collect completed orders before resetting the scene")
            db.execute(
                "UPDATE bin_allocations SET state='DISCARDED_BY_RESET',released=? WHERE released IS NULL",
                (time.time(),),
            )

    def placements(self, identifier):
        return [
            (
                row["sku"],
                SimpleNamespace(name=row["entity_name"], prefab_name=row["sku"]),
                row["bin_index"],
                tuple(json.loads(row["source"])),
            )
            for row in self.store.rows(
                """SELECT p.*,b.bin_index FROM placed_items p JOIN bins b USING(bin_id)
                WHERE p.order_id=? ORDER BY p.verified,p.rowid""",
                (identifier,),
            )
        ]

    def targets(self, identifier):
        return [
            tuple(json.loads(row["target"]))
            for row in self.store.rows(
                "SELECT target FROM placed_items WHERE order_id=? ORDER BY verified,rowid",
                (identifier,),
            )
        ]

    def delivered_entity_names(self):
        return {
            row["entity_name"]
            for row in self.store.rows("""
            SELECT p.entity_name FROM placed_items p JOIN inventory_items i USING(item_id)
            WHERE i.state IN ('PLACED','COLLECTED')
        """)
        }

    def recover(self):
        """Preserve evidence, invalidate old observations, quarantine physical ownership."""
        with self.store.transaction() as db:
            for job in self.all():
                if job.state == "COLLECTED" and job.reason != "已确认取货，箱位已释放":
                    self.update(job.order.order_id, reason="已确认取货，箱位已释放")
                allocation = self.allocation(job.order.order_id)
                active = bool(
                    allocation
                    and allocation.state
                    in ("ALLOCATED", "PAUSED", "WAITING_PICKUP", "RECOVERY_REQUIRED")
                )
                unfinished = bool(
                    self.store.rows(
                        "SELECT 1 FROM operations WHERE order_id=? AND state IN ('INTENT','UNCERTAIN')",
                        (job.order.order_id,),
                    )
                )
                if (
                    active
                    or job.state
                    in (
                        "DISPATCHING",
                        "RUNNING",
                        "PAUSED",
                        "WAITING_PICKUP",
                        "RECOVERY_REQUIRED",
                        "EXECUTION_UNCERTAIN",
                    )
                    or (
                        unfinished
                        and job.state
                        not in ("COLLECTED", "CANCELED", "CANCELLED", "FAILED")
                    )
                ):
                    self.update(
                        job.order.order_id,
                        state="RECOVERY_REQUIRED",
                        review_required=True,
                        reason="历史箱位或物理执行状态需核对；旧进度保留，不能自动重放",
                    )
                    if allocation and active:
                        self.allocation_state(job.order.order_id, "RECOVERY_REQUIRED")
            db.execute(
                "UPDATE inventory_items SET state='UNKNOWN',verification='REVIEW',executing=0 WHERE state IN ('AVAILABLE','RESERVED','CARRYING','UNKNOWN')"
            )
            # Only never-started orders may release old promises and await a new scan.
            db.execute(
                """UPDATE reservations SET released=?,updated=? WHERE released IS NULL AND order_id IN
                (SELECT order_id FROM orders WHERE state IN ('QUEUED','WAITING_STOCK','WAITING_BIN') AND review_required=0)""",
                (time.time(), time.time()),
            )
            return bool(self.store.rows("SELECT 1 FROM orders WHERE review_required=1"))
