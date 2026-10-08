"""Small Qt desktop panel for watching and exercising the order Action."""

import json
from pathlib import Path
from queue import Empty, Queue
import sys
import signal

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
from produce_order_interfaces.srv import ConfirmPickup
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

    def __init__(self):
        super().__init__()
        self._commands = Queue()
        self._active_goal = None
        self._pending_order_id = None
        self._online = None

    def submit(self, order_id: str, quantities: dict[str, int], prepare: bool) -> None:
        self._commands.put(("submit", order_id, quantities, prepare))

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
                            self._send(client, command)
                        elif command[0] == "cancel":
                            self._cancel()
                        elif command[0] == "recover":
                            self._recover(recovery)
                        elif command[0] == "pickup":
                            self._pickup(pickup, command[1])
                except Empty:
                    pass
                if not running:
                    break
                rclpy.spin_once(node, timeout_sec=0.1)
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
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()

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
        if not client.service_is_ready():
            self.notice.emit("取货服务尚未上线。")
            return
        request = ConfirmPickup.Request()
        request.order_id = order_id
        client.call_async(request).add_done_callback(
            lambda future: self.notice.emit(
                future.result().message if future.exception() is None
                else f"取货确认失败：{future.exception()}"
            )
        )

    def _recover(self, client) -> None:
        if not client.service_is_ready():
            self.notice.emit("恢复服务尚未上线；请检查订单服务是否运行。")
            return
        self.notice.emit("已请求恢复工作台，等待服务确认。")
        client.call_async(Trigger.Request()).add_done_callback(
            lambda future: self.notice.emit(
                future.result().message if future.exception() is None
                else f"恢复请求失败：{future.exception()}"
            )
        )


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
