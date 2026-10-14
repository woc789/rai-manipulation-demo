"""Persistent ROS 2 Action server for one produce-order workcell."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from math import isfinite

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.utilities import remove_ros_args
from rai.communication.ros2.connectors import ROS2Connector
import yaml
from std_msgs.msg import String
from std_srvs.srv import Trigger

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.srv import ConfirmPickup, SubmitOrder, CancelQueuedOrder
from produce_order_interfaces.msg import InventoryItem, InventorySnapshot
from geometry_msgs.msg import Point
from rosgraph_msgs.msg import Clock

from .fulfillment import BinManager
from .order import Order, OrderCancelled, OrderLine, fulfillment_mode, validate_order
from .runtime import Workstation
from .scheduling import OrderQueue
from .scheduling import QueuedOrder
from .fulfillment import OrderAllocation
from .persistence import WorkcellStore
from .business import BusinessCoordinator

CLOCK_QOS = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)


def order_from_goal(goal: ExecuteOrder.Goal) -> Order:
    return Order(
        order_id=goal.order_id,
        zone=goal.zone,
        lines=tuple(OrderLine(line.sku, int(line.quantity)) for line in goal.lines),
    )


def state_after_order(status: str, physical_uncertain: bool) -> tuple[str, str, bool]:
    """Require recovery for uncertain physical state, not every attempted plan."""
    if status == "COMPLETED":
        return "READY", "上一单已配货完成，箱位保留待取货；桌面库存未自动补满", True
    if status == "PAUSED":
        return "READY", "当前单已在验证装箱并退出后暂停，保留箱位和进度", True
    if physical_uncertain:
        return (
            "RECOVERY_REQUIRED",
            "机械动作结果尚未确认，现场需要核对；请点击“恢复工作台”重置场景",
            False,
        )
    return "READY", "订单在机械臂动作前结束，库存未改变，可以重新提交", True


class OrderActionServer(Node):
    """Accept one order at a time; publish progress and retain a JSON report."""

    def __init__(self, config_path: Path, scene_path: Path):
        super().__init__("produce_order_server")
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.scene_path = scene_path
        self.bin_manager = BinManager(self.config)
        self.queue = OrderQueue()
        self._dispatch_order_id = None
        self._dispatch_handle = None
        self._dispatch_started = None
        self._last_dispatch_feedback = None
        self._dispatch_timed_out = False
        watchdog = self.config.get("scheduler", {})
        self._accept_timeout = float(watchdog.get("accept_timeout_s", 15.0))
        self._feedback_timeout = float(watchdog.get("feedback_timeout_s", 180.0))
        self._result_timeout = float(watchdog.get("result_timeout_s", 900.0))
        if any(not isfinite(value) or value <= 0 for value in
               (self._accept_timeout, self._feedback_timeout, self._result_timeout)):
            raise ValueError("Scheduler watchdog timeouts must be finite and positive")
        self._scan_requested = False
        self._resume_review_after_scan = False
        self._inventory_ready = False
        self._order_contexts = {}
        self._lock = Lock()
        self._stopping = Event()
        self._busy = False
        self._scene_ready = False
        self._state = "INITIALIZING"
        self._state_reason = "正在启动感知服务并准备桌面库存"
        self._sim_seen = None
        self._clock_started = time.monotonic()
        self._sim_progress_seen = None
        self._sim_clock_value = None
        self._clock_subscription = self.create_subscription(Clock, "/clock", self._sim_clock, CLOCK_QOS)
        self._health_timer = self.create_timer(1.0, self._check_simulator)
        self._status_publisher = self.create_publisher(
            String, "/produce_orders/workcell_status",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )
        self._inventory_publisher = self.create_publisher(
            InventorySnapshot, "/produce_orders/inventory",
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self._recovery_service = self.create_service(
            Trigger, "/produce_orders/recover", self.recover_callback,
            callback_group=ReentrantCallbackGroup(),
        )
        self._pickup_service = self.create_service(
            ConfirmPickup, "/produce_orders/confirm_pickup", self.pickup_callback,
            callback_group=ReentrantCallbackGroup(),
        )
        self._submit_service = self.create_service(SubmitOrder, "/produce_orders/submit",
            self.submit_callback, callback_group=ReentrantCallbackGroup())
        self._queue_cancel_service = self.create_service(CancelQueuedOrder, "/produce_orders/cancel_queued",
            self.cancel_queued_callback, callback_group=ReentrantCallbackGroup())
        self._active_cancel_service = self.create_service(Trigger, "/produce_orders/cancel_active",
            self.cancel_active_callback, callback_group=ReentrantCallbackGroup())
        self._scan_service = self.create_service(Trigger, "/produce_orders/refresh_inventory",
            self.refresh_inventory_callback, callback_group=ReentrantCallbackGroup())
        self._replenish_service = self.create_service(Trigger, "/produce_orders/replenish",
            self.replenish_callback, callback_group=ReentrantCallbackGroup())
        self._status_timer = self.create_timer(1.0, self.publish_status)
        self.connector = ROS2Connector(
            "produce_order_runtime", executor_type="single_threaded"
        )
        self.workstation = Workstation(self.connector, self.config)
        self.store = None
        self._startup_review = False
        self._execution_bridge = None
        self._business_service = None
        self._business_gateway = None
        if self.config.get("persistence", {}).get("enabled", False):
            self.store = WorkcellStore(os.getenv("PRODUCE_STATE_DB", self.config["persistence"]["path"]))
            self._restore_business_state()
            self.workstation.durable_event_sink = self.store.event
            self.business = BusinessCoordinator(self)
            from .business_mcp import BusinessMCPServer
            self._business_gateway = BusinessMCPServer(self.business).__enter__()
            from produce_order_interfaces.srv import BusinessCommand
            self._business_service = self.create_service(BusinessCommand, "/produce_orders/business",
                self.business_callback, callback_group=ReentrantCallbackGroup())
        if self.config.get("execution", {}).get("backend") == "behavior_tree":
            if self.store is None:
                raise ValueError("Behavior-tree execution requires durable operation records")
            from .execution_bridge import ExecutionBridge
            self._execution_bridge = ExecutionBridge(self)
            self.workstation.execution_runner = self._execution_bridge.run
        from .mcp_server import WorkstationMCPServer
        from .agent_dispatch import AgentDispatcher
        self._mcp_gateway = WorkstationMCPServer(self.workstation).__enter__()
        self.workstation._mcp_server = self._mcp_gateway
        self._agent_dispatcher = AgentDispatcher(self, self.config,
            self.workstation.event, lambda: self.workstation.cancel_requested())
        self.workstation._agent_dispatcher = self._agent_dispatcher
        self._server = ActionServer(
            self,
            ExecuteOrder,
            "/produce_orders/execute",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
            callback_group=ReentrantCallbackGroup(),
        )
        self._dispatch_client = ActionClient(self, ExecuteOrder, "/produce_orders/execute",
                                            callback_group=ReentrantCallbackGroup())
        self._dispatch_timer = self.create_timer(1.0, self.dispatch_pending,
                                                 callback_group=ReentrantCallbackGroup())
        self._watchdog_timer = self.create_timer(1.0, self.check_dispatch_timeout,
                                                 callback_group=ReentrantCallbackGroup())
        Thread(target=self._prepare_initial_stock, daemon=True).start()

    def submit_callback(self, request, response):
        try:
            order = order_from_goal(request)
            validate_order(order, self.config)
            with self._lock:
                existing = self.queue.jobs.get(order.order_id)
                if existing is not None:
                    if existing.order != order or existing.priority != int(request.priority):
                        raise ValueError("订单号已有不同需求或优先级，不能覆盖")
                    response.success, response.state, response.message = True, existing.state, "相同订单已登记；返回原状态，不重复排队"
                    return response
                if order.order_id in self.bin_manager.orders or self.report_path(order.order_id).exists():
                    raise ValueError("订单号已执行或已有报告")
                job = self.queue.submit(order, int(request.priority))
                self._scan_requested = True
                self._persist_locked()
            response.success, response.state, response.message = True, job.state, "已登记，等待库存与箱位检查"
        except ValueError as exc:
            response.success, response.state, response.message = False, "REJECTED", str(exc)
        self.publish_status()
        return response

    def _sim_clock(self, _message):
        self._sim_seen = time.monotonic()
        value = (_message.clock.sec, _message.clock.nanosec)
        if value != self._sim_clock_value:
            self._sim_progress_seen = self._sim_seen
            self._sim_clock_value = value

    def _clock_problem(self):
        now = time.monotonic()
        if self._sim_seen is None:
            if now - getattr(self, "_clock_started", now) < float(self.config.get("clock_health", {}).get("startup_grace_s", 240)):
                return None
            return "从未收到仿真时钟"
        if now - self._sim_seen >= 15:
            return "仿真时间流已中断"
        progress = getattr(self, "_sim_progress_seen", self._sim_seen)
        if progress is not None and now - progress >= 15:
            return "仿真时钟停止推进（仿真可能已暂停）"
        return None

    def _check_simulator(self):
        problem = self._clock_problem()
        if problem is None:
            return
        with self._lock:
            if self._state not in ("INITIALIZING", "READY", "RUNNING", "SCANNING"):
                return
            identifier = self._dispatch_order_id
            self._scene_ready = False
            self._state = "SCENE_OFFLINE"
            self._state_reason = problem + "；禁止继续运动。请重启完整仿真，历史物理订单将在业务协作页要求核对。"
        if identifier:
            self._quarantine_dispatch("仿真时间流已中断，运动结果需要核对", identifier)
        self.publish_status()

    def _restore_business_state(self):
        snapshot = self.store.load()
        if not snapshot:
            return
        for row in snapshot.get("orders", []):
            order = Order(row["order_id"], row.get("zone", "A"),
                          tuple(OrderLine(line["sku"], line["quantity"]) for line in row["lines"]))
            self.queue._sequence += 1
            job = QueuedOrder(order, row["priority"], self.queue._sequence, total=order.total_items)
            self.queue.jobs[order.order_id] = job
            job.state, job.reason, job.completed = row["state"], row["reason"], row["completed"]
            if job.state == "COLLECTED":
                job.reason = "已确认取货，箱位已释放"
            checkpoint = snapshot.get("checkpoint", {})
            if checkpoint.get("order_id") == order.order_id:
                job.completed = max(job.completed, sum(checkpoint.get("completed_by_sku", {}).values()))
            verified = self.store.verified_progress(order.order_id)
            confirmed = sum(min(line.quantity, verified.get(line.sku, 0)) for line in order.lines)
            job.completed = max(job.completed, confirmed)
            job.review_required = row.get("review_required", False)
            job.finished_sequence = row.get("finished_sequence", 0)
            if job.state in ("DISPATCHING", "RUNNING", "PAUSED", "WAITING_PICKUP", "RECOVERY_REQUIRED"):
                job.state, job.review_required = "RECOVERY_REQUIRED", True
                job.reason = "重启前存在已分配或部分执行的物理任务；旧进度保留，须核对现场，不能自动重放"
                self._startup_review = True
        self.queue._finished_sequence = max((j.finished_sequence for j in self.queue.jobs.values()), default=0)
        for row in snapshot.get("allocations", []):
            job = self.queue.jobs.get(row["order_id"])
            if job is None and row.get("lines") and row["state"] in ("ALLOCATED", "PAUSED", "WAITING_PICKUP", "RECOVERY_REQUIRED"):
                job = self.queue.submit(Order(row["order_id"], row["zone"],
                    tuple(OrderLine(line["sku"], line["quantity"]) for line in row["lines"])))
                job.state, job.review_required = "RECOVERY_REQUIRED", True
                job.reason = "历史直接执行订单待核对，禁止重放"
                self._startup_review = True
            if job is not None and row["state"] in ("ALLOCATED", "PAUSED", "WAITING_PICKUP", "RECOVERY_REQUIRED"):
                job.state, job.review_required = "RECOVERY_REQUIRED", True
                job.reason = "历史箱位或物理执行状态需核对；不能因订单已失败就释放现场"
                self._startup_review = True
            if job and job.state == "RECOVERY_REQUIRED":
                allocation = OrderAllocation(job.order, tuple(row["bin_indices"]), state="RECOVERY_REQUIRED")
                self.bin_manager.orders[job.order.order_id] = allocation
                for index in allocation.bin_indices:
                    resource = self.bin_manager.bins[f"{job.order.zone}{index+1}"]
                    resource.state, resource.order_id = "RECOVERY_REQUIRED", job.order.order_id
        if any(row["state"] in ("INTENT", "UNCERTAIN") for row in self.store.operations()
               if self.queue.jobs.get(row["order_id"]) and self.queue.jobs[row["order_id"]].state not in ("COLLECTED", "FAILED", "CANCELED", "CANCELLED")):
            self._startup_review = True
        # Never restore old visual coordinates as AVAILABLE or RESERVED.
        if self._startup_review:
            self._state = "RECOVERY_REQUIRED"
            self._state_reason = "检测到历史订单现场未核对；请在业务协作页确认旧现场处理后再恢复"

    def _persist_locked(self, *, resolution=None):
        store = getattr(self, "store", None)
        if not isinstance(store, WorkcellStore):
            return
        orders = self.queue.snapshot()
        for row in orders:
            row["zone"] = self.queue.jobs[row["order_id"]].order.zone
        store.save(dict(schema=1, orders=orders, bins=self.bin_manager.snapshot(),
            allocations=[dict(order_id=key, zone=value.order.zone,
                lines=[dict(sku=line.sku, quantity=line.quantity) for line in value.order.lines],
                bin_indices=list(value.bin_indices), state=value.state,
                entities=[dict(sku=sku, name=entity.name, bin_index=index, source=list(source))
                          for sku, entity, index, source in value.entities])
                         for key, value in self.bin_manager.orders.items()],
            inventory=self.workstation.inventory.snapshot(),
            checkpoint=dict(order_id=getattr(self.workstation, "_active_order_id", None),
                completed_by_sku=dict(getattr(self.workstation, "_completed_by_sku", {})),
                entities=[dict(sku=sku, name=entity.name, bin_index=index, source=list(source))
                          for sku, entity, index, source in getattr(self.workstation, "_placed_entities", [])],
                physical_uncertain=bool(getattr(self.workstation, "physical_state_uncertain", False))),
            state=self._state, startup_review=self._startup_review), resolution=resolution)

    def persist_state(self):
        with self._lock:
            self._persist_locked()

    def business_callback(self, request, response):
        try:
            result = self.business.call(request.command, json.loads(request.arguments_json or "{}"))
            response.success, response.result_json = True, json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            response.success, response.result_json = False, json.dumps(dict(error=str(exc)), ensure_ascii=False)
        self.publish_status()
        return response

    def cancel_queued_callback(self, request, response):
        if getattr(request, "discard_partial", False):
            return self._discard_partial(request.order_id, response)
        try:
            with self._lock:
                self.queue.cancel(request.order_id, self.workstation.inventory)
            response.success, response.message = True, "排队订单已取消"
        except ValueError as exc:
            response.success, response.message = False, str(exc)
        self.publish_status()
        return response

    def _discard_partial(self, order_id, response):
        with self._lock:
            allocation = self.bin_manager.orders.get(order_id)
            if self._busy or self._dispatch_order_id or allocation is None or allocation.state != "PAUSED":
                response.success, response.message = False, "只能在空闲时明确丢弃安全暂停的部分订单"
                return response
            self._busy = True
        try:
            self.workstation.collect_order(allocation.order, allocation.entities)
            receipt = self.report_path(order_id).with_suffix(".discard.json")
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps(dict(order_id=order_id, delivery_state="DISCARDED",
                completed=len(allocation.entities), confirmed_at=datetime.now(timezone.utc).isoformat()),
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self._lock:
                self.bin_manager.discard_paused(order_id)
                self.workstation.inventory.release_order(order_id)
                self.queue.finish(order_id, "CANCELED", len(allocation.entities), "用户确认丢弃部分配货，已移走订单箱")
                self._order_contexts.pop(order_id, None)
                self._scan_requested = True
            response.success, response.message = True, "部分订单已丢弃，箱位释放；若现场异常仍需恢复"
        except Exception as exc:
            response.success, response.message = False, f"丢弃未完整成功，箱位仍保留，可重试：{exc}"
        finally:
            with self._lock:
                self._busy = False
            self.publish_status()
        return response

    def refresh_inventory_callback(self, _request, response):
        with self._lock:
            self._scan_requested = True
            self._resume_review_after_scan = True
        response.success, response.message = True, "已请求视觉复查；成功后允许等待核对的安全暂停单继续"
        return response

    def cancel_active_callback(self, _request, response):
        with self._lock:
            handle = self._dispatch_handle
        if handle is None:
            response.success, response.message = False, "当前没有调度器已接收的执行订单"
        else:
            handle.cancel_goal_async()
            response.success, response.message = True, "已请求取消，等待安全检查点；不是立即停止或已完成取消"
        return response

    def dispatch_pending(self):
        with self._lock:
            if self._busy or not self._scene_ready or self._dispatch_order_id:
                return
            if self._scan_requested:
                self._scan_requested = False
                self._busy = True
                self._state = "SCANNING"
                self._state_reason = "正在视觉复查库存，不进行机械臂运动"
                Thread(target=self._scan_stock, daemon=True).start()
                return
            if not self._inventory_ready:
                return
            if hasattr(self, "_agent_dispatcher") and not self._agent_dispatcher.ready():
                for waiting in self.queue.pending():
                    waiting.reason = ("等待 Agent 服务就绪；请检查 agent_server 与 /produce_agent/status "
                                      + self._agent_dispatcher.error)
                return
            job = self.queue.select(self.bin_manager, self.workstation.inventory)
            if job is None:
                return
            self._dispatch_order_id = job.order.order_id
            self._dispatch_started = time.monotonic()
            self._last_dispatch_feedback = self._dispatch_started
            self._dispatch_timed_out = False
        goal = ExecuteOrder.Goal()
        goal.order_id, goal.zone, goal.prepare_scene = job.order.order_id, job.order.zone, True
        from produce_order_interfaces.msg import OrderLine as LineMessage
        goal.lines = [LineMessage(sku=line.sku, quantity=line.quantity) for line in job.order.lines]
        try:
            future = self._dispatch_client.send_goal_async(goal,
                feedback_callback=lambda message, identifier=job.order.order_id:
                    self._queue_feedback(identifier, message))
            future.add_done_callback(lambda reply, identifier=job.order.order_id:
                                     self._dispatch_response(reply, identifier))
        except Exception as exc:
            self._dispatch_failed(str(exc))

    def _scan_stock(self):
        try:
            self.workstation.scan_inventory(list(self.config["aliases"]))
            self._inventory_ready = True
            with self._lock:
                if getattr(self, "_resume_review_after_scan", False):
                    for job in self.queue.jobs.values():
                        if job.review_required:
                            job.review_required = False
                            job.reason = "人工请求复查已完成，等待重新校验资源并恢复"
                    self._resume_review_after_scan = False
            reason = "视觉库存复查完成；观测不足的订单等待复查或补货"
        except Exception as exc:
            self._inventory_ready = False
            reason = f"视觉库存复查失败，可手动重试：{exc}"
            self.get_logger().error(reason)
            with self._lock:
                for job in self.queue.pending():
                    job.state, job.reason = "WAITING_STOCK", reason
        finally:
            with self._lock:
                self._busy = False
                self._state = "READY" if self._inventory_ready else "INVENTORY_ERROR"
                self._state_reason = reason
                if getattr(self.workstation, "physical_state_uncertain", False):
                    self._state, self._scene_ready = "RECOVERY_REQUIRED", False
                    self._state_reason = "观察姿态运动结果不明确，请核对机械臂后恢复工作台；不能只重试检测"
            self.publish_status()

    def check_dispatch_timeout(self):
        """Quarantine unknown execution; a timeout is NOT proof the robot stopped."""
        with self._lock:
            if not self._dispatch_order_id or self._dispatch_timed_out:
                return
            identifier = self._dispatch_order_id
        self._quarantine_dispatch("", identifier, check_timeouts=True)

    def _quarantine_dispatch(self, reason, identifier, *, check_timeouts=False):
        with self._lock:
            if not identifier or self._dispatch_order_id != identifier or self._dispatch_timed_out:
                return
            if check_timeouts:
                # Read heartbeat and transition state under the same mutex: a
                # just-arrived feedback/acceptance must not be timed out by an old read.
                reason = self._expired_dispatch_reason(time.monotonic())
                if reason is None:
                    return
            self._dispatch_timed_out = True
            self._scene_ready = False
            self._state = "RECOVERY_REQUIRED"
            self._state_reason = reason + "；已阻止后续调度，不释放预留、不自动重发。等待实际结果；无响应时须停机核对现场"
            job = self.queue.jobs[identifier]
            job.state, job.reason = "EXECUTION_UNCERTAIN", self._state_reason
            handle = self._dispatch_handle
        if handle is not None:
            try:
                handle.cancel_goal_async()
            except Exception as exc:
                self.get_logger().error(f"Watchdog cancellation failed: {exc}")
        self.publish_status()

    def _expired_dispatch_reason(self, now):
        """Caller holds _lock. Monotonic deadlines do not depend on ROS sim time."""
        age = now - self._dispatch_started
        if self._dispatch_handle is None and age > self._accept_timeout:
            return "执行 Action 接收响应超时"
        if self._dispatch_handle is not None:
            if age > self._result_timeout:
                return "执行 Action 总时限超时"
            if now - self._last_dispatch_feedback > self._feedback_timeout:
                return "执行 Action 反馈停滞超时"
        return None

    def _dispatch_response(self, future, identifier):
        try:
            with self._lock:
                if self._dispatch_order_id != identifier:
                    return
            handle = future.result()
            with self._lock:
                if self._dispatch_order_id != identifier:
                    return
            if not handle.accepted:
                self._dispatch_failed("执行服务拒绝请求，需手动复查后重试", confirmed_rejected=True)
                return
            with self._lock:
                self._dispatch_handle = handle
                self._last_dispatch_feedback = time.monotonic()
                timed_out = self._dispatch_timed_out
                if not timed_out:
                    self.queue.jobs[identifier].state = "RUNNING"
            handle.get_result_async().add_done_callback(
                lambda result: self._queue_result(result, identifier))
            if timed_out:
                handle.cancel_goal_async()
        except Exception as exc:
            self._quarantine_dispatch("执行响应异常：" + str(exc), identifier)

    def _dispatch_failed(self, reason, *, confirmed_rejected=False):
        if not confirmed_rejected:
            self._quarantine_dispatch("执行通信异常：" + reason, self._dispatch_order_id)
            return
        with self._lock:
            if self._dispatch_order_id:
                job = self.queue.jobs[self._dispatch_order_id]
                if job.order.order_id not in self.bin_manager.orders:
                    self.workstation.inventory.release_order(job.order.order_id)
                else:
                    self._scene_ready = False
                    self._state, self._state_reason = "RECOVERY_REQUIRED", "执行通信结果不明确，不能自动重试运动"
                job.state, job.reason = "FAILED", reason
            self._dispatch_order_id = None
            self._dispatch_handle = None
        self.publish_status()

    def _queue_feedback(self, order_id, message):
        with self._lock:
            if self._dispatch_order_id == order_id:
                self._last_dispatch_feedback = time.monotonic()
                job = self.queue.jobs[order_id]
                job.completed = max(job.completed, message.feedback.completed)

    def _should_yield(self, order_id):
        """Called ONLY after placement verification and retreat, never during a motion."""
        with self._lock:
            current = self.queue.jobs[order_id]
            for job in self.queue.pending():
                if job.priority <= current.priority:
                    break
                allocation = self.bin_manager.orders.get(job.order.order_id)
                if allocation is None and not self.bin_manager.can_allocate(job.order.zone):
                    continue
                _, missing = self.workstation.inventory.reserve_order(job.order)
                if not missing:
                    return True
        return False

    def _queue_result(self, future, identifier):
        try:
            with self._lock:
                if self._dispatch_order_id != identifier:
                    return
            result = future.result().result
            with self._lock:
                if self._dispatch_order_id != identifier:
                    return
                job = self.queue.jobs[self._dispatch_order_id]
                self.queue.finish(job.order.order_id, result.status, result.completed,
                                  result.error or "配货完成，等待取货")
                self._dispatch_order_id, self._dispatch_handle = None, None
                self._scan_requested = True
                if self._dispatch_timed_out:
                    self._scene_ready = False
                    self._state = "RECOVERY_REQUIRED"
                    self._state_reason = "超时订单结果已收到，仍需核对现场；先处理待取货/暂停订单，再恢复工作台"
        except Exception as exc:
            self._quarantine_dispatch("执行结果异常：" + str(exc), identifier)
        self.publish_status()

    def start_maintenance(self, kind, identifier, response):
        request = self.store.request(identifier)
        expected = "request_scene_reset" if kind == "recover" else "request_replenishment"
        if not request or request["state"] != "APPROVED" or request.get("execution_state") != "ADMITTING" or request["kind"] != expected:
            raise ValueError("Maintenance has no valid durable admission")
        callback = self.recover_callback if kind == "recover" else self.replenish_callback
        return callback(Trigger.Request(), response, request_id=identifier)

    def replenish_callback(self, _request, response, *, request_id=None):
        with self._lock:
            if self._busy or not self._scene_ready or self._dispatch_order_id:
                response.success, response.message = False, "只有现场就绪且无执行订单时才能补货"
                return response
            self._busy, self._scene_ready = True, False
            self._state, self._state_reason = "REPLENISHING", "只替换桌面剩余货源，保留已完成订单箱"
        Thread(target=self._replenish_stock, args=(request_id,), daemon=True).start()
        response.success, response.message = True, "补货准备已启动，等待 READY 和视觉复查"
        self.publish_status()
        return response

    def _replenish_stock(self, request_id=None):
        success, error = False, ""
        try:
            self.workstation.replenish_source(self.scene_path)
        except Exception as exc:
            error = str(exc)
            self._set_state("RECOVERY_REQUIRED", f"补货未完整成功，需检查现场：{exc}")
        else:
            success = True
            with self._lock:
                self._scan_requested = True
            self._set_state("READY", "桌面已补货，等待视觉复查", ready=True)
        finally:
            with self._lock:
                self._busy = False
            if request_id:
                self.store.execution_result(request_id, "SUCCEEDED" if success else "FAILED", error)
            self.publish_status()

    def publish_status(self) -> None:
        with self._lock:
            self._persist_locked()
            status = {
                "state": self._state,
                "reason": self._state_reason,
                "can_submit": self._scene_ready and self._inventory_ready and not self._busy and self.bin_manager.can_allocate("A"),
                "can_recover": not getattr(self, "_startup_review", False) and not self._busy and not self._dispatch_order_id and self._state not in (
                    "INITIALIZING", "REPLENISHING", "RECOVERING"
                ) and self.bin_manager.can_reset(),
                "can_pickup": not self._busy,
                "operation_mode": self.config.get("operation_mode", "classification"),
                "bins": self.bin_manager.snapshot(),
                "queue_enabled": True,
                "can_register": len(self.queue.pending()) < self.queue.max_pending,
                "orders": self.queue.snapshot(),
                "inventory_summary": self.workstation.inventory.summary() if hasattr(self, "workstation") else {},
                "execution_backend": self.config.get("execution", {}).get("backend", "legacy_agent"),
                "business_enabled": isinstance(getattr(self, "store", None), WorkcellStore),
                "startup_review": bool(getattr(self, "_startup_review", False)),
                "approval_requests": self.store.requests() if isinstance(getattr(self, "store", None), WorkcellStore) else [],
            }
        self._status_publisher.publish(String(data=json.dumps(status, ensure_ascii=False)))
        self.publish_inventory()

    def publish_inventory(self):
        if not hasattr(self, "workstation"):
            return
        snapshot = self.workstation.inventory.snapshot()
        message = InventorySnapshot()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.config["camera"]["target_frame"]
        message.epoch = snapshot["epoch"]
        message.scene_ready = self.workstation._scene_prepared and self._state not in (
            "INITIALIZING", "RECOVERING", "REPLENISHING")
        message.persistence = snapshot["persistence"]
        for row in snapshot["items"]:
            item = InventoryItem()
            for key in ("item_id", "sku", "frame_id", "observed_unix_sec", "age_sec", "stale",
                        "classification_verified", "state", "order_id", "observation_source"):
                setattr(item, key, row[key])
            item.position = Point(x=row["position"][0], y=row["position"][1], z=row["position"][2])
            message.items.append(item)
        self._inventory_publisher.publish(message)

    def _set_state(self, state: str, reason: str, *, ready: bool = False) -> None:
        if ready and hasattr(self, "_clock_started"):
            problem = self._clock_problem()
            if problem:
                state, reason, ready = "SCENE_OFFLINE", problem + "；请恢复仿真后核对现场", False
        with self._lock:
            self._state = state
            self._state_reason = reason
            self._scene_ready = ready
        self.publish_status()

    def _prepare_initial_stock(self) -> None:
        """Make inventory visible before a customer submits an order."""
        if getattr(self, "_startup_review", False):
            self._set_state("RECOVERY_REQUIRED", "历史物理任务待核对；业务协作页可明确确认丢弃旧现场并重建")
            return
        try:
            self.workstation.wait_until_ready(with_scene=True)
            self.workstation.prepare_scene(self.scene_path)
        except Exception as exc:
            self.get_logger().error(f"Could not preload mixed inventory: {exc}")
            self._set_state("RECOVERY_REQUIRED", f"库存初始化失败：{exc}")
            return
        self._set_state("READY", "混合库存已就绪，可以提交订单", ready=True)
        with self._lock:
            self._scan_requested = True
        self.get_logger().info("Mixed table inventory is ready: four of each SKU")

    def pickup_callback(self, request, response):
        with self._lock:
            if self._busy or self._state in ("INITIALIZING", "RECOVERING"):
                response.success, response.message = False, "工作台忙，请等待当前操作结束"
                return response
            try:
                allocation = self.bin_manager.pickup(request.order_id)
            except ValueError as exc:
                response.success, response.message = False, str(exc)
                return response
            self._busy = True
        self.publish_status()
        try:
            self.workstation.collect_order(allocation.order, allocation.entities)
            # Keep execution reports immutable; record delivery separately.
            receipt_path = self.report_path(request.order_id).with_suffix(".pickup.json")
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(json.dumps({
                "order_id": request.order_id, "delivery_state": "COLLECTED",
                "bin_ids": [f"{allocation.order.zone}{i + 1}" for i in allocation.bin_indices],
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with self._lock:
                self.bin_manager.confirm_pickup(request.order_id)
                self._scan_requested = True
                if request.order_id in self.queue.jobs:
                    self.queue.jobs[request.order_id].state = "COLLECTED"
                    self.queue.jobs[request.order_id].reason = "已确认取货，箱位已释放"
            response.success = True
            response.message = f"订单 {request.order_id} 已取货，箱位释放；桌面库存保持不变"
        except Exception as exc:
            # Never free ownership on a partial or failed simulator cleanup.
            response.success, response.message = False, f"取货清理失败，箱位仍保留，可检查后重试：{exc}"
            self.get_logger().error(response.message)
        finally:
            with self._lock:
                self._busy = False
            self.publish_status()
        return response

    def recover_callback(self, _request, response, *, request_id=None):
        with self._lock:
            if getattr(self, "_startup_review", False) and request_id is None:
                response.success, response.message = False, "请先在业务协作页确认旧现场处理；重启不等于旧订单已交付"
                return response
            if self._busy or self._dispatch_order_id or self._state in ("INITIALIZING", "REPLENISHING", "RECOVERING"):
                response.success = False
                response.message = "工作台正在执行或准备中，暂不能重置场景"
                return response
            if not self.bin_manager.can_reset():
                response.success = False
                response.message = "请先确认取走所有已完成订单；重置不能清空待取货订单"
                return response
            self._busy = True
            self._scene_ready = False
            self._state = "RECOVERING"
            self._state_reason = "正在清理并重新生成库存与分箱"
        self.publish_status()
        Thread(target=self._recover_scene, args=(request_id,), daemon=True).start()
        response.success = True
        response.message = "已开始重置场景；等待状态变为 READY 后再提交订单"
        return response

    def _recover_scene(self, request_id=None) -> None:
        success, error = False, ""
        try:
            self.workstation.wait_until_ready(with_scene=True)
            self.workstation.prepare_scene(self.scene_path)
        except Exception as exc:
            error = str(exc)
            self.get_logger().error(f"Workcell recovery failed: {exc}")
            self._set_state("RECOVERY_REQUIRED", f"场景恢复失败：{exc}")
        else:
            with self._lock:
                if request_id and getattr(self, "_startup_review", False):
                    for job in self.queue.jobs.values():
                        if job.state == "RECOVERY_REQUIRED":
                            job.state, job.review_required = "FAILED", False
                            job.reason = "操作员确认重建旧现场；保留历史进度，不记为已交付"
                    self._startup_review = False
                self.bin_manager.reset()
                self._dispatch_timed_out = False
                self._inventory_ready = False
                self._scan_requested = True
            success = True
            self._set_state("READY", "场景已重置，库存已就绪", ready=True)
        finally:
            with self._lock:
                self._busy = False
            if request_id:
                self.store.execution_result(request_id, "SUCCEEDED" if success else "FAILED", error)
            self.publish_status()

    @staticmethod
    def report_path(order_id: str) -> Path:
        return (
            Path.home()
            / ".local/share/produce_order_workstation/reports"
            / f"{order_id}.json"
        )

    def goal_callback(self, goal: ExecuteOrder.Goal) -> GoalResponse:
        try:
            if not goal.prepare_scene:
                raise ValueError("Agent orders require prepare_scene=true")
            validate_order(order_from_goal(goal), self.config)
        except ValueError as exc:
            self.get_logger().warning(f"Rejected invalid order: {exc}")
            return GoalResponse.REJECT
        with self._lock:
            if self._dispatch_order_id and self._dispatch_order_id != goal.order_id:
                return GoalResponse.REJECT
            if goal.order_id in self.queue.jobs and self._dispatch_order_id != goal.order_id:
                self.get_logger().warning("Queued orders must be started by the scheduler")
                return GoalResponse.REJECT
            if goal.order_id in self.queue.jobs and order_from_goal(goal) != self.queue.jobs[goal.order_id].order:
                self.get_logger().warning("Execution goal differs from the registered order")
                return GoalResponse.REJECT
            if self.queue.pending() and self._dispatch_order_id != goal.order_id:
                self.get_logger().warning("Use submit service while the business queue is enabled")
                return GoalResponse.REJECT
            if self._busy:
                self.get_logger().warning("Rejected order: workcell is busy")
                return GoalResponse.REJECT
            if not self._scene_ready:
                self.get_logger().warning("Rejected order: mixed inventory is not ready")
                return GoalResponse.REJECT
            previous = self.bin_manager.orders.get(goal.order_id)
            resuming = previous is not None and previous.state == "PAUSED" and self._dispatch_order_id == goal.order_id
            if self.report_path(goal.order_id).exists() and not resuming:
                self.get_logger().warning("Rejected order: order ID already has a report")
                return GoalResponse.REJECT
            try:
                if fulfillment_mode(self.config) == "fulfillment":
                    _, missing = self.workstation.inventory.reserve_order(order_from_goal(goal))
                    if missing:
                        self.get_logger().warning(f"Insufficient fresh observations for full order reservation: {missing}")
                        return GoalResponse.REJECT
                allocation = self.bin_manager.orders.get(goal.order_id)
                if allocation is not None and allocation.state == "PAUSED" and self._dispatch_order_id == goal.order_id:
                    allocation.state = "ALLOCATED"
                    for index in allocation.bin_indices:
                        self.bin_manager.bins[f"{goal.zone}{index + 1}"].state = "ALLOCATED"
                else:
                    if self.report_path(goal.order_id).exists():
                        raise ValueError("Order ID already has a report")
                    allocation = self.bin_manager.allocate(order_from_goal(goal))
            except ValueError as exc:
                self.workstation.inventory.release_order(goal.order_id)
                self.get_logger().warning(f"Rejected order: {exc}")
                return GoalResponse.REJECT
            self._busy = True
            self._scene_ready = False
            self._state = "RUNNING"
            bin_ids = "/".join(f"{goal.zone}{i + 1}" for i in allocation.bin_indices)
            self._state_reason = f"正在执行订单 {goal.order_id}，箱位 {bin_ids}"
        self.publish_status()
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle) -> CancelResponse:
        self._publish_feedback(goal_handle, {
            "stage": "CANCEL_REQUESTED", "detail": "Waiting for a safe item boundary"})
        return CancelResponse.ACCEPT

    def _publish_feedback(self, goal_handle, entry: dict) -> None:
        feedback = ExecuteOrder.Feedback()
        feedback.stage = entry["stage"]
        feedback.sku = entry.get("sku", "")
        feedback.completed = int(entry.get("completed", sum(self.workstation._completed_by_sku.values())))
        feedback.total = int(entry.get("total", sum(line.quantity for line in goal_handle.request.lines)))
        feedback.detail_json = json.dumps(entry, ensure_ascii=False)
        goal_handle.publish_feedback(feedback)

    def execute_callback(self, goal_handle) -> ExecuteOrder.Result:
        order = order_from_goal(goal_handle.request)
        report_path = self.report_path(order.order_id)
        self.workstation.events = []
        self.workstation.event_sink = lambda entry: self._publish_feedback(
            goal_handle, entry
        )
        self.workstation.cancel_requested = lambda: goal_handle.is_cancel_requested or (
            getattr(self, "_stopping", None) is not None and self._stopping.is_set())
        try:
            allocation = self.bin_manager.orders[order.order_id]
            self.workstation.begin_order(order, bin_index=allocation.bin_indices[0])
            self.workstation.require_order_reservation = fulfillment_mode(self.config) == "fulfillment" or order.order_id in self.queue.jobs
            self.workstation.yield_requested = (lambda: self._should_yield(order.order_id)) if order.order_id in self.queue.jobs else lambda: False
            context = self._order_contexts.pop(order.order_id, None)
            if context:
                self.workstation._placed_entities = context["placed_entities"]
                self.workstation._completed_by_sku = context["completed_by_sku"]
                self.workstation._used_placement_targets = context.get("used_placement_targets", [])
                self.workstation.motion_attempted = context["motion_attempted"]
                self.workstation.events = context["events"] + self.workstation.events
                self.workstation.synchronize_scene()
            else:
                self.workstation.ensure_order_bins(order, allocation.bin_indices)
            self.workstation.event(order.order_id, "STARTING")
            self.workstation.check_cancel()
            self.workstation.event(order.order_id, "USING_PRELOADED_SCENE")
            self.workstation.check_cancel()
            result = self.workstation.run(order)
        except OrderCancelled as exc:
            self.workstation.event(order.order_id, "ORDER_CANCELLED", reason=str(exc))
            result = self._failure_result(order, "CANCELLED", str(exc))
        except Exception as exc:
            self.get_logger().error(f"Order {order.order_id} failed: {exc}")
            self.workstation.event(order.order_id, "ORDER_FAILED", reason=str(exc))
            result = self._failure_result(order, "FAILED", str(exc))
        finally:
            self.workstation.event_sink = None
            self.workstation.cancel_requested = lambda: False
            self.workstation.require_order_reservation = False
            self.workstation.yield_requested = lambda: False

        try:
            allocation = self.bin_manager.orders[order.order_id]
            scene_uncertain = False
            physical_uncertain = getattr(self.workstation, "physical_state_uncertain", self.workstation.motion_attempted)
            if result["status"] not in ("COMPLETED", "PAUSED") and not physical_uncertain:
                try:
                    self.workstation.remove_order_bins(order, allocation.bin_indices)
                except Exception as exc:
                    scene_uncertain = True
                    self.get_logger().error(f"Failed to remove unused order bin: {exc}")
            result["operation_mode"] = self.config.get("operation_mode", "classification")
            result["bin_ids"] = [f"{order.zone}{i + 1}" for i in allocation.bin_indices]
            result["delivery_state"] = "WAITING_PICKUP" if result["status"] == "COMPLETED" else result["status"]
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            response = ExecuteOrder.Result()
            response.status = result["status"]
            response.completed = result["completed"]
            response.requested = result["requested"]
            response.error = result["error"] or ""
            response.report_path = str(report_path)
            if result["status"] in ("COMPLETED", "PAUSED"):
                goal_handle.succeed()
            elif result["status"] == "CANCELLED":
                goal_handle.canceled()
            else:
                goal_handle.abort()
            return response
        finally:
            state, reason, ready = state_after_order(
                result["status"], physical_uncertain or scene_uncertain
            )
            if result["status"] not in ("COMPLETED", "PAUSED"):
                self.workstation.inventory.finish_failed(order.order_id,
                    physical_uncertain or scene_uncertain)
                if not physical_uncertain and not scene_uncertain:
                    self.workstation.inventory.release_order(order.order_id)
            with self._lock:
                if result["status"] == "PAUSED":
                    self._order_contexts[order.order_id] = dict(
                        placed_entities=list(self.workstation._placed_entities),
                        completed_by_sku=dict(self.workstation._completed_by_sku),
                        used_placement_targets=list(self.workstation._used_placement_targets),
                        motion_attempted=self.workstation.motion_attempted,
                        events=list(self.workstation.events))
                self.bin_manager.finish(order.order_id, result["status"],
                                        physical_uncertain or scene_uncertain,
                                        self.workstation._placed_entities)
                self._state, self._state_reason, self._scene_ready = state, reason, ready
                if getattr(self, "_dispatch_timed_out", False):
                    self._state, self._scene_ready = "RECOVERY_REQUIRED", False
                    self._state_reason = "执行曾超时，等待结果回传并人工核对现场；禁止自动继续"
                self._busy = False
            self.publish_status()

    def _failure_result(self, order: Order, status: str, error: str) -> dict:
        return {
            "order_id": order.order_id,
            "status": status,
            "completed": len(self.workstation._placed_entities),
            "requested": order.total_items,
            "lines": [{"sku": line.sku, "quantity": line.quantity} for line in order.lines],
            "error": error,
            "events": self.workstation.events,
        }

    def close(self) -> None:
        self.persist_state()
        if self._execution_bridge is not None:
            self._execution_bridge.close()
        if self._business_gateway is not None:
            self._business_gateway.close()
        if hasattr(self, "business") and hasattr(self.business, "_agent_client"):
            self.business._agent_client.destroy()
        self._mcp_gateway.close()
        self._agent_dispatcher.close()
        if self.store is not None:
            self.store.close()
        self._dispatch_client.destroy()
        self._server.destroy()
        self.connector.shutdown()
        self.destroy_node()


def main() -> None:
    share = Path(get_package_share_directory("produce_order_workstation"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=share / "config/workstation.yaml")
    parser.add_argument("--scene", type=Path, default=share / "config/demo_scene.yaml")
    args = parser.parse_args(remove_ros_args()[1:])
    rclpy.init()
    server = OrderActionServer(args.config, args.scene)
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(server)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        server._stopping.set()
        executor.shutdown()
        server.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
