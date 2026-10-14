"""Deterministic business queue. The ROS server serializes this object with its lock."""

from dataclasses import dataclass, field

from .order import Order

STATE_LABELS = {"QUEUED": "排队", "WAITING_STOCK": "等待有效库存观测或补货",
    "WAITING_BIN": "等待空闲箱位", "DISPATCHING": "正在派发", "RUNNING": "执行中",
    "PAUSED": "暂停", "WAITING_PICKUP": "配货完成，尚未取货", "COLLECTED": "已经确认取货，箱位已释放",
    "RECOVERY_REQUIRED": "需要现场核对", "FAILED": "失败", "CANCELED": "已取消", "CANCELLED": "已取消"}


@dataclass
class QueuedOrder:
    order: Order
    priority: int
    sequence: int
    state: str = "QUEUED"
    reason: str = "等待调度"
    missing: dict = field(default_factory=dict)
    completed: int = 0
    total: int = 0
    finished_sequence: int = 0
    review_required: bool = False


class OrderQueue:
    def __init__(self, max_pending=100):
        self.jobs = {}
        self.max_pending = max_pending
        self._sequence = 0
        self._finished_sequence = 0

    def submit(self, order, priority=0):
        if order.order_id in self.jobs:
            raise ValueError("订单号已登记")
        if not isinstance(priority, int) or not 0 <= priority <= 10:
            raise ValueError("优先级必须为 0～10")
        if len(self.pending()) >= self.max_pending:
            raise ValueError("等待队列已满")
        self._sequence += 1
        job = QueuedOrder(order, priority, self._sequence, total=order.total_items)
        self.jobs[order.order_id] = job
        return job

    def pending(self):
        return sorted((job for job in self.jobs.values()
                       if job.state in ("QUEUED", "WAITING_STOCK", "WAITING_BIN", "PAUSED")
                       and not job.review_required),
                      key=lambda job: (-job.priority, job.sequence))

    def select(self, bins, inventory):
        for job in self.pending():
            allocation = bins.orders.get(job.order.order_id)
            resuming = allocation is not None and allocation.state == "PAUSED"
            if not resuming and not bins.can_allocate(job.order.zone):
                job.state, job.reason = "WAITING_BIN", "等待空闲箱位"
                continue
            _, missing = inventory.reserve_order(job.order)
            job.missing = missing
            if missing:
                ambiguous = set(missing) & inventory.ambiguous_skus()
                job.state = "WAITING_STOCK"
                job.reason = (
                    "同一位置被识别为不同商品（" + "、".join(sorted(ambiguous))
                    + "）；请调整遮挡或视角并视觉复查，必要时人工核对；未确认前不抓取"
                    if ambiguous else
                    "可用视觉观测不足，等待复查或补货；不等同于确认缺货"
                )
                continue
            job.state, job.reason = "DISPATCHING", "资源已预留，正在发送执行请求"
            return job
        return None

    def cancel(self, order_id, inventory):
        job = self.jobs.get(order_id)
        if job is None or job not in self.pending() or job.state == "PAUSED" or job.completed:
            raise ValueError("只能取消尚未开始调度的订单")
        inventory.release_order(order_id)
        job.state, job.reason = "CANCELED", "用户取消排队订单"

    def snapshot(self):
        return [dict(order_id=job.order.order_id, priority=job.priority, state=job.state,
                     state_label=STATE_LABELS.get(job.state, job.state),
                     reason="已经确认取货，箱位已释放" if job.state == "COLLECTED" else job.reason,
                     missing=job.missing, completed=job.completed,
                     review_required=job.review_required,
                     total=job.total, finished_sequence=job.finished_sequence,
                     lines=[dict(sku=line.sku, quantity=line.quantity)
                                           for line in job.order.lines])
                for job in sorted(self.jobs.values(), key=lambda job: job.sequence)]

    def finish(self, order_id, status, completed, reason):
        if status not in ("COMPLETED", "PAUSED", "FAILED", "CANCELLED", "CANCELED"):
            raise ValueError("执行返回了未知业务状态，需核对现场")
        job = self.jobs[order_id]
        job.state = "WAITING_PICKUP" if status == "COMPLETED" else status
        job.reason, job.completed = reason, completed
        job.review_required = status == "PAUSED" and reason.startswith("[REVIEW_REQUIRED]")
        if job.review_required:
            job.reason = "已装箱进度保留；请点击视觉复查后继续，或确认丢弃部分订单。原因：" + reason.removeprefix("[REVIEW_REQUIRED] ")
        if status != "PAUSED":
            self._finished_sequence += 1
            job.finished_sequence = self._finished_sequence
