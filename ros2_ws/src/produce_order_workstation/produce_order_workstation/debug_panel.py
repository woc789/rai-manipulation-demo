"""Small Qt desktop panel for watching and exercising the order Action."""

import json
from pathlib import Path
from queue import Empty, Queue
import sys
import signal
import time

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import String
from std_srvs.srv import Trigger

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine
from produce_order_interfaces.msg import InventorySnapshot
from produce_order_interfaces.srv import ConfirmPickup, SubmitOrder, CancelQueuedOrder
from .operator_panel import OperatorPanel


class RosWorker(QThread):
    """Spin ROS outside Qt's GUI thread and transfer updates through signals."""

    connected = pyqtSignal(bool)
    accepted = pyqtSignal(bool)
    feedback = pyqtSignal(str, int, int, str)
    finished = pyqtSignal(str, int, int, str, str)
    notice = pyqtSignal(str)
    workcell_status = pyqtSignal(str, str, bool, bool)
    resources = pyqtSignal(str)
    inventory = pyqtSignal(str)
    registered = pyqtSignal(bool, str, str)
    queue_cleared = pyqtSignal(int, int)
    operation_finished = pyqtSignal(str, bool, str)

    def __init__(self):
        super().__init__()
        self._commands = Queue()
        self._active_goal = None
        self._pending_order_id = None
        self._online = None
        self._queue_enabled = False
        self._event_counts = {}
        self._clearing_queue = False
        self._clear_queue_batch = None
        self._service_waits = {}

    def submit(self, order_id: str, quantities: dict[str, int], prepare: bool, priority=0) -> None:
        self._commands.put(("submit", order_id, quantities, prepare, priority))

    def cancel_queued(self, order_id, discard_partial=False):
        self._commands.put(("cancel_queued", order_id, discard_partial))

    def clear_queue(self, order_ids):
        self._commands.put(("clear_queue", tuple(order_ids)))

    def refresh_inventory(self):
        self._commands.put(("refresh_inventory",))

    def replenish(self):
        self._commands.put(("replenish",))

    def cancel(self) -> None:
        self._commands.put(("cancel",))

    def recover(self) -> None:
        self._commands.put(("recover",))

    def pickup(self, order_id: str) -> None:
        self._commands.put(("pickup", order_id))

    def stop(self) -> None:
        self._commands.put(("stop",))

    def run(self) -> None:
        # The Qt main thread owns process signals; stop ROS via the command queue.
        rclpy.init(args=None, signal_handler_options=SignalHandlerOptions.NO)
        node = Node("produce_order_debug_panel")
        client = ActionClient(node, ExecuteOrder, "/produce_orders/execute")
        recovery = node.create_client(Trigger, "/produce_orders/recover")
        pickup = node.create_client(ConfirmPickup, "/produce_orders/confirm_pickup")
        registration = node.create_client(SubmitOrder, "/produce_orders/submit")
        queue_cancel = node.create_client(CancelQueuedOrder, "/produce_orders/cancel_queued")
        active_cancel = node.create_client(Trigger, "/produce_orders/cancel_active")
        refresh = node.create_client(Trigger, "/produce_orders/refresh_inventory")
        replenish = node.create_client(Trigger, "/produce_orders/replenish")
        node.create_subscription(String, "/produce_orders/events", self._on_event, 10)
        node.create_subscription(
            String, "/produce_orders/workcell_status", self._on_workcell_status,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )
        node.create_subscription(
            InventorySnapshot, "/produce_orders/inventory", self._on_inventory,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        running = True
        last_check = 0.0
        try:
            while running and rclpy.ok():
                try:
                    while True:
                        command = self._commands.get_nowait()
                        if command[0] == "stop":
                            running = False
                            break
                        if command[0] == "submit":
                            if self._queue_enabled:
                                self._register(registration, command)
                            else:
                                self._send(client, command[:4])
                        elif command[0] == "cancel":
                            if self._queue_enabled:
                                self._trigger(active_cancel, "cancel")
                            else:
                                self._cancel()
                        elif command[0] == "recover":
                            self._recover(recovery)
                        elif command[0] == "pickup":
                            self._pickup(pickup, command[1])
                        elif command[0] == "cancel_queued":
                            self._cancel_queued(queue_cancel, command[1], command[2])
                        elif command[0] == "clear_queue":
                            self._clear_queue(queue_cancel, command[1])
                        elif command[0] == "refresh_inventory":
                            self._trigger(refresh)
                        elif command[0] == "replenish":
                            self._trigger(replenish)
                except Empty:
                    pass
                if not running:
                    break
                rclpy.spin_once(node, timeout_sec=0.1)
                self._check_clear_queue_timeout()
                self._check_service_timeouts()
                now = node.get_clock().now().nanoseconds / 1e9
                if now - last_check >= 1.0:
                    online = client.server_is_ready()
                    if online != self._online:
                        self._online = online
                        self.connected.emit(online)
                    last_check = now
        finally:
            client.destroy()
            node.destroy_client(recovery)
            node.destroy_client(pickup)
            for service in (registration, queue_cancel, active_cancel, refresh, replenish):
                node.destroy_client(service)
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()

    def _register(self, client, command):
        _, order_id, quantities, _, priority = command
        if not client.service_is_ready():
            self.registered.emit(False, order_id, "订单登记服务离线")
            return
        request = SubmitOrder.Request(order_id=order_id, zone="A", priority=priority,
            lines=[OrderLine(sku=sku, quantity=count) for sku, count in quantities.items() if count])
        def done(future):
            try:
                response = future.result()
                self.registered.emit(response.success, order_id, response.message)
            except Exception as exc:
                self.registered.emit(False, order_id, str(exc))
        client.call_async(request).add_done_callback(done)

    def _trigger(self, client, operation="maintenance"):
        self._request_service(client, Trigger.Request(), operation)

    def _request_service(self, client, request, operation):
        if not client.service_is_ready():
            message = "业务服务离线，请求未发送"
            self.notice.emit(message)
            self.operation_finished.emit(operation, False, message)
            return
        try:
            future = client.call_async(request)
        except Exception as exc:
            self.notice.emit(str(exc))
            self.operation_finished.emit(operation, False, str(exc))
            return
        self._service_waits[future] = (operation, client, time.monotonic() + 10.0)
        def done(reply):
            pending = self._service_waits.pop(reply, None)
            if pending is None:
                return
            try:
                response = reply.result()
                success, message = response.success, response.message
            except Exception as exc:
                success, message = False, f"服务请求异常：{exc}"
            self.notice.emit(message)
            self.operation_finished.emit(operation, success, message)
        future.add_done_callback(done)

    def _check_service_timeouts(self):
        now = time.monotonic()
        for future, (operation, client, deadline) in tuple(self._service_waits.items()):
            if now > deadline:
                self._service_waits.pop(future, None)
                client.remove_pending_request(future)
                message = "服务响应超过 10 秒，结果未确认；请查看状态，勿重复点击"
                self.notice.emit(message)
                self.operation_finished.emit(operation, False, message)

    def _cancel_queued(self, client, order_id, discard_partial=False):
        self._request_service(client,
            CancelQueuedOrder.Request(order_id=order_id, discard_partial=discard_partial), "cancel_queued")

    def _clear_queue(self, client, order_ids):
        """Reuse guarded cancellation: never cancel/discard a running or partial job."""
        ids = list(dict.fromkeys(order_ids))
        if self._clearing_queue:
            self.notice.emit("正在清除队列，请等待")
            return
        if not client.service_is_ready():
            self.notice.emit("队列服务离线，未清除订单")
            self.queue_cleared.emit(0, len(ids))
            return
        self._clearing_queue = True
        batch = {"remaining": set(ids), "counts": [0, 0], "deadline": time.monotonic() + 10.0}
        self._clear_queue_batch = batch
        if not ids:
            self._finish_queue_clear_entry(batch, None, True)
            return
        for identifier in ids:
            request = CancelQueuedOrder.Request(order_id=identifier, discard_partial=False)
            def done(future, order_id=identifier):
                try:
                    success = future.result().success
                except Exception:
                    success = False
                self._finish_queue_clear_entry(batch, order_id, success)
            try:
                client.call_async(request).add_done_callback(done)
            except Exception:
                self._finish_queue_clear_entry(batch, identifier, False)

    def _finish_queue_clear_entry(self, batch, identifier, success):
        if self._clear_queue_batch is not batch:
            return
        if identifier is not None:
            if identifier not in batch["remaining"]:
                return
            batch["remaining"].remove(identifier)
            batch["counts"][0 if success else 1] += 1
        if not batch["remaining"]:
            self._clear_queue_batch = None
            self._clearing_queue = False
            cleared, skipped = batch["counts"]
            self.notice.emit(f"已清除 {cleared} 份待执行订单；跳过或未确认 {skipped} 份，请查看队列状态")
            self.queue_cleared.emit(cleared, skipped)

    def _check_clear_queue_timeout(self):
        batch = self._clear_queue_batch
        if batch and time.monotonic() > batch["deadline"]:
            for identifier in tuple(batch["remaining"]):
                self._finish_queue_clear_entry(batch, identifier, False)

    def _on_event(self, message):
        if not self._queue_enabled:
            return
        try:
            entry = json.loads(message.data)
            if entry.get("order_id"):
                previous = self._event_counts.get(entry["order_id"], (0, 0))
                counts = (entry.get("completed", previous[0]), entry.get("total", previous[1]))
                self._event_counts[entry["order_id"]] = counts
                self.feedback.emit(entry["stage"], *counts, message.data)
        except (ValueError, KeyError):
            pass

    def _send(self, client: ActionClient, command: tuple) -> None:
        _, order_id, quantities, prepare = command
        if not client.server_is_ready():
            self.notice.emit("订单服务尚未上线；请先启动仿真与订单服务。")
            self.accepted.emit(False)
            return
        goal = ExecuteOrder.Goal()
        goal.order_id = order_id
        goal.zone = "A"
        goal.prepare_scene = prepare
        goal.lines = []
        for sku, quantity in quantities.items():
            if quantity:
                line = OrderLine()
                line.sku = sku
                line.quantity = quantity
                goal.lines.append(line)
        self.notice.emit(f"已提交订单 {order_id}，等待服务确认。")
        self._pending_order_id = order_id
        future = client.send_goal_async(goal, feedback_callback=self._on_feedback)
        future.add_done_callback(self._on_goal_response)

    def _on_goal_response(self, future) -> None:
        try:
            handle = future.result()
            if not handle.accepted:
                report = (
                    Path.home()
                    / ".local/share/produce_order_workstation/reports"
                    / f"{self._pending_order_id}.json"
                )
                if report.exists():
                    self.notice.emit(
                        f"订单号 {self._pending_order_id} 已有报告；请换一个订单号。"
                    )
                else:
                    self.notice.emit(
                        "订单未被接收：请查看上方工作台状态。若正在准备库存，"
                        "等待就绪；若需要恢复，点击“恢复工作台”。"
                    )
                self.accepted.emit(False)
                return
            self._active_goal = handle
            self.accepted.emit(True)
            self.notice.emit("订单已接收，开始执行。")
            handle.get_result_async().add_done_callback(self._on_result)
        except Exception as exc:
            self.notice.emit(f"提交失败：{exc}")
            self.accepted.emit(False)

    def _on_feedback(self, message) -> None:
        data = message.feedback
        self.feedback.emit(
            data.stage, data.completed, data.total, data.detail_json
        )

    def _on_result(self, future) -> None:
        try:
            result = future.result().result
            self.finished.emit(
                result.status,
                result.completed,
                result.requested,
                result.error,
                result.report_path,
            )
        except Exception as exc:
            self.notice.emit(f"读取结果失败：{exc}")
        finally:
            self._active_goal = None

    def _cancel(self) -> None:
        if self._active_goal is None:
            self.notice.emit("当前没有已接收的订单可取消。")
            return
        self._active_goal.cancel_goal_async()
        self.notice.emit("已请求取消；正在执行的抓放会先到安全检查点。")

    def _on_workcell_status(self, message: String) -> None:
        try:
            status = json.loads(message.data)
        except (TypeError, ValueError):
            return
        self._queue_enabled = bool(status.get("queue_enabled"))
        self.workcell_status.emit(
            status.get("state", "UNKNOWN"), status.get("reason", ""),
            bool(status.get("can_submit")), bool(status.get("can_recover")),
        )
        self.resources.emit(message.data)

    def _on_inventory(self, message):
        self.inventory.emit(json.dumps({
            "epoch": message.epoch, "scene_ready": message.scene_ready,
            "persistence": message.persistence,
            "items": [{"item_id": item.item_id, "sku": item.sku, "state": item.state,
                       "order_id": item.order_id, "frame_id": item.frame_id,
                       "position": [item.position.x, item.position.y, item.position.z],
                       "age_sec": item.age_sec, "stale": item.stale,
                       "classification_verified": item.classification_verified}
                      for item in message.items]}, ensure_ascii=False))

    def _pickup(self, client, order_id: str) -> None:
        request = ConfirmPickup.Request()
        request.order_id = order_id
        self._request_service(client, request, "pickup")

    def _recover(self, client) -> None:
        self.notice.emit("已请求恢复工作台，等待服务确认。")
        self._request_service(client, Trigger.Request(), "recover")


class DebugPanel(OperatorPanel):
    def __init__(self, **kwargs):
        super().__init__(RosWorker, **kwargs)


def main() -> None:
    app = QApplication(sys.argv)
    panel = DebugPanel()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: panel.close())
    panel.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
