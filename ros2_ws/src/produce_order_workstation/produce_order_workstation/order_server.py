"""Persistent ROS 2 Action server for one produce-order workcell."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Lock, Thread

from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.utilities import remove_ros_args
from rai.communication.ros2.connectors import ROS2Connector
import yaml
from std_msgs.msg import String
from std_srvs.srv import Trigger

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.srv import ConfirmPickup, SubmitOrder, CancelQueuedOrder
from produce_order_interfaces.msg import InventoryItem, InventorySnapshot
from geometry_msgs.msg import Point

from .fulfillment import BinManager
from .order import Order, OrderCancelled, OrderLine, fulfillment_mode, validate_order
from .runtime import Workstation
from .scheduling import OrderQueue


def order_from_goal(goal: ExecuteOrder.Goal) -> Order:
    return Order(
        order_id=goal.order_id,
        zone=goal.zone,
        lines=tuple(OrderLine(line.sku, int(line.quantity)) for line in goal.lines),
    )


def state_after_order(status: str, motion_attempted: bool) -> tuple[str, str, bool]:
    """Only an untouched scene can immediately accept a new order."""
    if status == "COMPLETED":
        return "READY", "上一单已配货完成，箱位保留待取货；桌面库存未自动补满", True
    if status == "PAUSED":
        return "READY", "当前单已在验证装箱并退出后暂停，保留箱位和进度", True
    if motion_attempted:
        return (
            "RECOVERY_REQUIRED",
            "订单中尝试过机械臂动作，现场状态可能改变；请点击“恢复工作台”重置场景",
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
        self._scan_requested = False
        self._inventory_ready = False
        self._order_contexts = {}
        self._lock = Lock()
        self._busy = False
        self._scene_ready = False
        self._state = "INITIALIZING"
        self._state_reason = "正在启动感知服务并准备桌面库存"
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
        Thread(target=self._prepare_initial_stock, daemon=True).start()

    def submit_callback(self, request, response):
        try:
            order = order_from_goal(request)
            validate_order(order, self.config)
            with self._lock:
                if order.order_id in self.bin_manager.orders or self.report_path(order.order_id).exists():
                    raise ValueError("订单号已执行或已有报告")
                job = self.queue.submit(order, int(request.priority))
                self._scan_requested = True
            response.success, response.state, response.message = True, job.state, "已登记，等待库存与箱位检查"
        except ValueError as exc:
            response.success, response.state, response.message = False, "REJECTED", str(exc)
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
        response.success, response.message = True, "已请求视觉复查，将在工作台空闲后进行"
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
            job = self.queue.select(self.bin_manager, self.workstation.inventory)
            if job is None:
                return
            self._dispatch_order_id = job.order.order_id
        goal = ExecuteOrder.Goal()
        goal.order_id, goal.zone, goal.prepare_scene = job.order.order_id, job.order.zone, True
        from produce_order_interfaces.msg import OrderLine as LineMessage
        goal.lines = [LineMessage(sku=line.sku, quantity=line.quantity) for line in job.order.lines]
        try:
            future = self._dispatch_client.send_goal_async(goal,
                feedback_callback=lambda message, identifier=job.order.order_id:
                    self._queue_feedback(identifier, message))
            future.add_done_callback(self._dispatch_response)
        except Exception as exc:
            self._dispatch_failed(str(exc))

    def _scan_stock(self):
        try:
            self.workstation.scan_inventory(list(self.config["aliases"]))
            self._inventory_ready = True
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
                self._state, self._state_reason = "READY", reason
            self.publish_status()

    def _dispatch_response(self, future):
        try:
            handle = future.result()
            if not handle.accepted:
                self._dispatch_failed("执行服务拒绝请求，需手动复查后重试")
                return
            with self._lock:
                self._dispatch_handle = handle
                self.queue.jobs[self._dispatch_order_id].state = "RUNNING"
            handle.get_result_async().add_done_callback(self._queue_result)
        except Exception as exc:
            self._dispatch_failed(str(exc))

    def _dispatch_failed(self, reason):
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

    def _queue_result(self, future):
        try:
            result = future.result().result
            with self._lock:
                job = self.queue.jobs[self._dispatch_order_id]
                self.queue.finish(job.order.order_id, result.status, result.completed,
                                  result.error or "配货完成，等待取货")
                self._dispatch_order_id, self._dispatch_handle = None, None
                self._scan_requested = True
        except Exception as exc:
            self._dispatch_failed(str(exc))
        self.publish_status()

    def replenish_callback(self, _request, response):
        with self._lock:
            if self._busy or not self._scene_ready or self._dispatch_order_id:
                response.success, response.message = False, "只有现场就绪且无执行订单时才能补货"
                return response
            self._busy, self._scene_ready = True, False
            self._state, self._state_reason = "REPLENISHING", "只替换桌面剩余货源，保留已完成订单箱"
        Thread(target=self._replenish_stock, daemon=True).start()
        response.success, response.message = True, "补货准备已启动，等待 READY 和视觉复查"
        self.publish_status()
        return response

    def _replenish_stock(self):
        try:
            self.workstation.replenish_source(self.scene_path)
        except Exception as exc:
            self._set_state("RECOVERY_REQUIRED", f"补货未完整成功，需检查现场：{exc}")
        else:
            with self._lock:
                self._scan_requested = True
            self._set_state("READY", "桌面已补货，等待视觉复查", ready=True)
        finally:
            with self._lock:
                self._busy = False
            self.publish_status()

    def publish_status(self) -> None:
        with self._lock:
            status = {
                "state": self._state,
                "reason": self._state_reason,
                "can_submit": self._scene_ready and not self._busy and self.bin_manager.can_allocate("A"),
                "can_recover": not self._busy and self._state not in (
                    "INITIALIZING", "REPLENISHING", "RECOVERING"
                ) and self.bin_manager.can_reset(),
                "can_pickup": not self._busy,
                "operation_mode": self.config.get("operation_mode", "classification"),
                "bins": self.bin_manager.snapshot(),
                "queue_enabled": True,
                "can_register": len(self.queue.pending()) < self.queue.max_pending,
                "orders": self.queue.snapshot(),
                "inventory_summary": self.workstation.inventory.summary() if hasattr(self, "workstation") else {},
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
        with self._lock:
            self._state = state
            self._state_reason = reason
            self._scene_ready = ready
        self.publish_status()

    def _prepare_initial_stock(self) -> None:
        """Make inventory visible before a customer submits an order."""
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

    def recover_callback(self, _request, response):
        with self._lock:
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
        Thread(target=self._recover_scene, daemon=True).start()
        response.success = True
        response.message = "已开始重置场景；等待状态变为 READY 后再提交订单"
        return response

    def _recover_scene(self) -> None:
        try:
            self.workstation.wait_until_ready(with_scene=True)
            self.workstation.prepare_scene(self.scene_path)
        except Exception as exc:
            self.get_logger().error(f"Workcell recovery failed: {exc}")
            self._set_state("RECOVERY_REQUIRED", f"场景恢复失败：{exc}")
        else:
            with self._lock:
                self.bin_manager.reset()
                self._inventory_ready = False
                self._scan_requested = True
            self._set_state("READY", "场景已重置，库存已就绪", ready=True)
        finally:
            with self._lock:
                self._busy = False
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
        self.workstation.cancel_requested = lambda: goal_handle.is_cancel_requested
        try:
            allocation = self.bin_manager.orders[order.order_id]
            self.workstation.begin_order(order, bin_index=allocation.bin_indices[0])
            self.workstation.require_order_reservation = fulfillment_mode(self.config) == "fulfillment" or order.order_id in self.queue.jobs
            self.workstation.yield_requested = (lambda: self._should_yield(order.order_id)) if order.order_id in self.queue.jobs else lambda: False
            context = self._order_contexts.pop(order.order_id, None)
            if context:
                self.workstation._placed_entities = context["placed_entities"]
                self.workstation._completed_by_sku = context["completed_by_sku"]
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
            if result["status"] not in ("COMPLETED", "PAUSED") and not self.workstation.motion_attempted:
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
                result["status"], self.workstation.motion_attempted or scene_uncertain
            )
            if result["status"] not in ("COMPLETED", "PAUSED"):
                self.workstation.inventory.finish_failed(order.order_id,
                    self.workstation.motion_attempted or scene_uncertain)
                if not self.workstation.motion_attempted and not scene_uncertain:
                    self.workstation.inventory.release_order(order.order_id)
            with self._lock:
                if result["status"] == "PAUSED":
                    self._order_contexts[order.order_id] = dict(
                        placed_entities=list(self.workstation._placed_entities),
                        completed_by_sku=dict(self.workstation._completed_by_sku),
                        motion_attempted=self.workstation.motion_attempted,
                        events=list(self.workstation.events))
                self.bin_manager.finish(order.order_id, result["status"],
                                        self.workstation.motion_attempted or scene_uncertain,
                                        self.workstation._placed_entities)
                self._state, self._state_reason, self._scene_ready = state, reason, ready
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
        executor.shutdown()
        server.close()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
