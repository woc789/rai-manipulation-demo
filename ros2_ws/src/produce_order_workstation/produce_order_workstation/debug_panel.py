"""Small Qt desktop panel for watching and exercising the order Action."""

from datetime import datetime
import json
from pathlib import Path
from queue import Empty, Queue
import sys

from ament_index_python.packages import get_package_share_directory
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from std_srvs.srv import Trigger

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine
import yaml

from .order import (
    Order,
    OrderLine as BusinessOrderLine,
    capacity_usage,
    plan_placements,
    readable_error,
    validate_order,
)


class RosWorker(QThread):
    """Spin ROS outside Qt's GUI thread and transfer updates through signals."""

    connected = pyqtSignal(bool)
    accepted = pyqtSignal(bool)
    feedback = pyqtSignal(str, int, int, str)
    finished = pyqtSignal(str, int, int, str, str)
    notice = pyqtSignal(str)
    workcell_status = pyqtSignal(str, str, bool, bool)

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

    def stop(self) -> None:
        self._commands.put(("stop",))

    def run(self) -> None:
        rclpy.init(args=None)
        node = Node("produce_order_debug_panel")
        client = ActionClient(node, ExecuteOrder, "/produce_orders/execute")
        recovery = node.create_client(Trigger, "/produce_orders/recover")
        node.create_subscription(
            String, "/produce_orders/workcell_status", self._on_workcell_status,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
        )
        running = True
        last_check = 0.0
        try:
            while running:
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
            node.destroy_node()
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


class DebugPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("生鲜拣选工作站 · 调试面板")
        self.resize(850, 620)
        config_path = (
            Path(get_package_share_directory("produce_order_workstation"))
            / "config/workstation.yaml"
        )
        self.config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self._order_active = False
        self._active_order_id = ""
        self._online = False
        self._workcell_can_submit = False
        self._workcell_can_recover = False
        self._has_previous_attempt = False
        self.worker = RosWorker()
        self._build_ui()
        self.order_id.textChanged.connect(self._refresh_order_preview)
        for spin in self.quantity_spins.values():
            spin.valueChanged.connect(self._refresh_order_preview)
        self._refresh_order_preview()
        self.worker.connected.connect(self._set_connected)
        self.worker.accepted.connect(self._set_accepted)
        self.worker.feedback.connect(self._on_feedback)
        self.worker.finished.connect(self._on_result)
        self.worker.notice.connect(self._append)
        self.worker.workcell_status.connect(self._set_workcell_status)
        self.worker.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(16)
        header = QLabel("生鲜订单拣选工作站")
        header.setObjectName("title")
        root.addWidget(header)
        root.addWidget(QLabel("调试面板 · RGB-D 识别/三维定位 → Agent 选工具 → ROS 2 抓放"))

        top = QHBoxLayout()
        self.connection = QLabel("● 正在连接订单服务")
        self.connection.setObjectName("connection")
        self.status = QLabel("待提交")
        self.status.setObjectName("status")
        top.addWidget(self.connection)
        top.addStretch()
        top.addWidget(self.status)
        root.addLayout(top)
        self.workcell_state = QLabel("工作台状态：等待服务消息")
        self.workcell_state.setWordWrap(True)
        root.addWidget(self.workcell_state)

        order_box = QGroupBox("① 输入订单")
        form = QFormLayout(order_box)
        self.order_id = QLineEdit()
        self.order_id.setReadOnly(True)
        self._new_id()
        form.addRow("订单号", self.order_id)
        quantities = QGridLayout()
        self.quantity_spins = {}
        for index, (sku, label) in enumerate(
            (("corn", "玉米"), ("carrot", "胡萝卜"), ("apple", "苹果"), ("tomato", "番茄"))
        ):
            spin = QSpinBox()
            spin.setRange(
                0,
                min(
                    self.config["zones"]["A"]["bin_capacity_items"],
                    self.config["stock"]["initial_counts"][sku],
                ),
            )
            row, column = divmod(index, 2)
            bin_id = self.config["zones"]["A"]["sku_bins"][sku] + 1
            quantities.addWidget(QLabel(f"{label} → A{bin_id}"), row, column * 2)
            quantities.addWidget(spin, row, column * 2 + 1)
            self.quantity_spins[sku] = spin
        form.addRow("商品数量", quantities)
        form.addRow("订单方式", QLabel("自动生成订单号；只填写四类商品的数量（0 表示不需要）"))
        self.capacity_status = QLabel()
        self.capacity_status.setWordWrap(True)
        form.addRow("容量校验", self.capacity_status)
        self.bin_preview = QLabel()
        self.bin_preview.setWordWrap(True)
        form.addRow("分箱预览", self.bin_preview)
        form.addRow("场景", QLabel("服务启动时预置 16 件混合库存；Agent 调用视觉定位后选择抓取目标"))
        buttons = QHBoxLayout()
        self.submit_button = QPushButton("提交订单")
        self.submit_button.setObjectName("primary")
        self.submit_button.clicked.connect(self._submit)
        self.cancel_button = QPushButton("请求取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.worker.cancel)
        buttons.addWidget(self.submit_button)
        buttons.addWidget(self.cancel_button)
        form.addRow(buttons)
        recovery_buttons = QHBoxLayout()
        self.retry_button = QPushButton("重新提交本单")
        self.retry_button.setToolTip("保持当前商品数量，使用新的订单号重新提交")
        self.retry_button.clicked.connect(self._retry)
        self.retry_button.setEnabled(False)
        self.recover_button = QPushButton("恢复工作台（重置场景）")
        self.recover_button.setToolTip("清理并重建仿真物品和收货箱；不会重启 O3DE 进程")
        self.recover_button.clicked.connect(self._recover)
        self.recover_button.setEnabled(False)
        recovery_buttons.addWidget(self.retry_button)
        recovery_buttons.addWidget(self.recover_button)
        form.addRow("故障处理", recovery_buttons)
        root.addWidget(order_box)

        progress_box = QGroupBox("② 执行状态")
        progress_layout = QVBoxLayout(progress_box)
        self.stage = QLabel("等待提交")
        self.progress = QProgressBar()
        self.progress.setValue(0)
        progress_layout.addWidget(self.stage)
        progress_layout.addWidget(self.progress)
        self.last_result = QLabel("上一单：暂无")
        self.last_result.setWordWrap(True)
        progress_layout.addWidget(self.last_result)
        self.failure_reason = QLabel("")
        self.failure_reason.setWordWrap(True)
        self.failure_reason.setStyleSheet("color: #fca5a5;")
        progress_layout.addWidget(self.failure_reason)
        root.addWidget(progress_box)

        log_box = QGroupBox("③ 实时事件与输出")
        log_layout = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.document().setMaximumBlockCount(1000)
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        root.addWidget(log_box, 1)
        self.setStyleSheet(
            """
            QWidget { background: #111827; color: #e5e7eb; font-size: 14px; }
            QGroupBox { border: 1px solid #374151; border-radius: 10px;
                        margin-top: 12px; padding: 14px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
            QLineEdit, QSpinBox, QPlainTextEdit { background: #1f2937;
                border: 1px solid #4b5563; border-radius: 6px; padding: 7px; }
            QPushButton { background: #374151; border: none; border-radius: 7px;
                          padding: 10px 18px; }
            QPushButton#primary { background: #0e7490; color: white; font-weight: 700; }
            QPushButton:disabled { background: #27303f; color: #6b7280; }
            QPushButton:hover:enabled { background: #0891b2; }
            QLabel#title { font-size: 25px; font-weight: 750; color: #67e8f9; }
            QLabel#connection { color: #fbbf24; }
            QLabel#status { color: #a7f3d0; font-weight: 700; }
            QProgressBar { border: 1px solid #4b5563; border-radius: 6px;
                           background: #1f2937; text-align: center; height: 20px; }
            QProgressBar::chunk { background: #10b981; border-radius: 5px; }
            """
        )

    def _new_id(self) -> None:
        self.order_id.setText(datetime.now().strftime("debug-%Y%m%d-%H%M%S-%f"))

    def _current_order(self) -> Order:
        return Order(
            self.order_id.text().strip(),
            "A",
            tuple(
                BusinessOrderLine(sku, spin.value())
                for sku, spin in self.quantity_spins.items()
                if spin.value()
            ),
        )

    def _set_form_enabled(self, enabled: bool) -> None:
        self.order_id.setEnabled(enabled)
        for spin in self.quantity_spins.values():
            spin.setEnabled(enabled)

    def _refresh_order_preview(self, _=None) -> None:
        if self._order_active:
            return
        order = self._current_order()
        usage = capacity_usage(order, self.config)
        self.stage.setText("尚未提交")
        self.progress.setMaximum(100)
        self.progress.setValue(0)
        if not order.lines:
            detail = "至少选择一件商品。"
        else:
            try:
                validate_order(order, self.config)
            except ValueError as exc:
                detail = str(exc)
            else:
                detail = ""
        if detail:
            self.capacity_status.setText(
                f"不可提交 · 已选 {usage.items_needed} 件。{detail}"
            )
            self.capacity_status.setStyleSheet("color: #fca5a5;")
            self.bin_preview.setText("请调整数量后查看分箱方案。")
            self.submit_button.setEnabled(False)
            self.retry_button.setEnabled(False)
            self.status.setText("订单配置无效")
            return
        self.capacity_status.setText(
            f"可提交 · 已选 {usage.items_needed} 件，桌面预置混合库存；"
            f"使用 {usage.bins_needed}/{usage.bins_available} 个分类箱；"
            f"每类现有 {self.config['stock']['initial_counts']['corn']} 件库存。"
        )
        self.capacity_status.setStyleSheet("color: #86efac;")
        bins = [0 for _ in range(usage.bins_available)]
        labels = {"corn": "玉米", "carrot": "胡萝卜", "apple": "苹果", "tomato": "番茄"}
        for placement in plan_placements(order, self.config):
            bins[placement.bin_index] += 1
        sku_bins = self.config["zones"]["A"]["sku_bins"]
        self.bin_preview.setText(
            "  ·  ".join(
                f"A{index + 1} {labels[sku]} ×{bins[index]}"
                for sku, index in sorted(sku_bins.items(), key=lambda item: item[1])
            )
        )
        self.submit_button.setEnabled(
            self._online and self._workcell_can_submit and not self._order_active
        )
        self.retry_button.setEnabled(
            self._has_previous_attempt and self._online
            and self._workcell_can_submit and not self._order_active
        )
        self.status.setText("待提交")

    def _submit(self) -> None:
        order = self._current_order()
        try:
            validate_order(order, self.config)
        except ValueError as exc:
            self._append(f"订单不可提交：{exc}")
            self._refresh_order_preview()
            return
        quantities = {line.sku: line.quantity for line in order.lines}
        self._order_active = True
        self._active_order_id = order.order_id
        self._set_form_enabled(False)
        self.progress.setValue(0)
        self.failure_reason.setText("")
        self.status.setText("提交中")
        self.stage.setText("等待订单服务确认")
        self.submit_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.worker.submit(order.order_id, quantities, True)

    def _retry(self) -> None:
        self._new_id()
        self._submit()

    def _recover(self) -> None:
        if self._order_active or not self._workcell_can_recover:
            return
        self.recover_button.setEnabled(False)
        self.worker.recover()

    def _set_connected(self, online: bool) -> None:
        self._online = online
        self.connection.setText("● 订单服务在线" if online else "● 订单服务离线")
        self.connection.setStyleSheet(
            "color: #34d399;" if online else "color: #fbbf24;"
        )
        self._refresh_order_preview()

    def _set_workcell_status(self, state: str, reason: str, can_submit: bool, can_recover: bool) -> None:
        self._workcell_can_submit = can_submit
        self._workcell_can_recover = can_recover
        self.workcell_state.setText(f"工作台状态：{state} · {reason}")
        self.recover_button.setEnabled(can_recover and not self._order_active)
        self._refresh_order_preview()

    def _set_accepted(self, accepted: bool) -> None:
        self.cancel_button.setEnabled(accepted)
        if accepted:
            self.status.setText("执行中")
        else:
            self._order_active = False
            self._has_previous_attempt = True
            self._set_form_enabled(True)
            self._refresh_order_preview()
            self.status.setText("未被接收")
            self.stage.setText("订单未被接收；请查看下方提示")

    def _on_feedback(self, stage: str, completed: int, total: int, detail: str) -> None:
        self.stage.setText(f"当前阶段：{stage}")
        self.status.setText(stage)
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(completed)
        try:
            event = json.loads(detail)
            if stage == "AGENT_TOOL_CALL":
                argument = event.get("arguments", {})
                target = argument.get("detection_id", argument.get("sku", ""))
                self._append(f"Agent 选择工具：{event['tool']} {target}".strip())
            elif stage == "AGENT_TOOL_RESULT" and event.get("tool") == "get_object_positions":
                result = event.get("result", {})
                self._append(
                    f"视觉定位 {result.get('sku')}：{len(result.get('detections', []))} 个候选，"
                    f"坐标系 {result.get('frame')}"
                )
            elif stage == "ITEM_COMPLETED":
                self._append(
                    f"{event['sku']} 已装 {event['sku_completed']}/{event['sku_requested']}；"
                    f"整单 {event['completed']}/{event['total']}，"
                    f"剩余 {event['remaining_by_sku']}"
                )
            self._append(json.dumps(event, ensure_ascii=False))
        except json.JSONDecodeError:
            self._append(detail)

    def _on_result(
        self, status: str, completed: int, requested: int, error: str, report: str
    ) -> None:
        self.last_result.setText(
            f"上一单 {self._active_order_id}：{status}，完成 {completed}/{requested}"
        )
        self.failure_reason.setText(readable_error(error) if error else "")
        self._append(f"最终结果：{status} · {completed}/{requested}")
        if error:
            self._append(f"原因：{readable_error(error)}")
        self._append(f"报告：{report}")
        self._order_active = False
        self._has_previous_attempt = True
        self._set_form_enabled(True)
        self.cancel_button.setEnabled(False)
        self._new_id()
        self._refresh_order_preview()
        self.recover_button.setEnabled(self._workcell_can_recover)

    def _append(self, message: str) -> None:
        self.log.appendPlainText(message)

    def closeEvent(self, event) -> None:
        self.worker.stop()
        self.worker.wait(3000)
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    panel = DebugPanel()
    panel.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
