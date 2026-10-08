"""Operator presentation only: ROS transport stays in debug_panel.RosWorker."""

from datetime import datetime
import json
from pathlib import Path
from time import monotonic

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
import yaml

from .order import Order, OrderLine, validate_order, readable_error
from .panel_components import BinCard, Card, Metric, NAMES, ProduceGlyph, StageStrip, STYLE, STATE_NAMES, label


class OperatorPanel(QWidget):
    """Draft, active task, and server resources are three independent states."""

    def __init__(self, worker_factory, *, start_worker=True, config=None, report_dir=None):
        super().__init__()
        if config is None:
            from ament_index_python.packages import get_package_share_directory
            path = Path(get_package_share_directory("produce_order_workstation")) / "config/workstation.yaml"
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.config = config
        self.report_dir = report_dir or Path.home() / ".local/share/produce_order_workstation/reports"
        self._online = False
        self._last_status = 0.0
        self._order_active = False
        self._accepted = False
        self._active_order_id = ""
        self._can_submit = self._can_recover = self._can_pickup = False
        self._valid = False
        self._state = "UNKNOWN"
        self._bins = []
        self._started = start_worker
        self.worker = worker_factory()
        self.setWindowTitle("生鲜配货 · ROS 2 工作台")
        self.setObjectName("operatorPanel")
        self.resize(1280, 900)
        self.setMinimumSize(1000, 700)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._new_id()
        self._refresh_history()
        self._restore_last_task()
        self._refresh_draft()
        for signal, slot in (
            (self.worker.connected, self._set_connected),
            (self.worker.accepted, self._set_accepted),
            (self.worker.feedback, self._on_feedback),
            (self.worker.finished, self._on_result),
            (self.worker.notice, self._append),
            (self.worker.workcell_status, self._set_workcell_status),
            (self.worker.resources, self._set_resources),
        ):
            signal.connect(slot)
        self.history_timer = QTimer(self)
        self.history_timer.timeout.connect(self._refresh_history)
        self.history_timer.start(5000)
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self._check_status)
        self.health_timer.start(1000)
        if start_worker:
            self.worker.start()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(166)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 28, 16, 22)
        side.setSpacing(12)
        side.addWidget(label("FRESH / 配货", "brand"))
        side.addSpacing(24)
        self.nav_buttons = []
        for text, index in (("工作台", 0), ("订单记录", 0), ("运行日志", 2)):
            button = QPushButton(text)
            button.setObjectName("nav")
            button.setCheckable(True)
            button.setChecked(text == "工作台")
            button.clicked.connect(lambda _, i=index, name=text: self._navigate(name, i))
            side.addWidget(button)
            self.nav_buttons.append(button)
        side.addSpacing(20)
        side.addWidget(label("后续扩展", "sideHint"))
        for text in ("库存中心 · 规划中", "任务调度 · 规划中"):
            button = QPushButton(text)
            button.setObjectName("nav")
            button.setEnabled(False)
            side.addWidget(button)
        side.addStretch()
        self.recover_button = QPushButton("恢复工作台")
        self.recover_button.clicked.connect(self._recover)
        side.addWidget(self.recover_button)
        side.addWidget(label("ROS 2 × Agent", "sideHint"))
        root.addWidget(sidebar)
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        content.setObjectName("operatorPanel")
        page = QVBoxLayout(content)
        page.setContentsMargins(24, 22, 24, 22)
        page.setSpacing(16)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.addWidget(label("生鲜配货工作台", "pageTitle"))
        heading.addWidget(label("订单 / 自动配货", "muted"))
        header.addLayout(heading)
        header.addStretch()
        self.connection = label("● 订单服务离线", "connection")
        self.connection.setMaximumHeight(36)
        header.addWidget(self.connection)
        page.addLayout(header)
        metrics = QHBoxLayout()
        self.metrics = [Metric(t) for t in ("工作台", "执行中", "待取货", "空箱位")]
        for metric in self.metrics:
            metrics.addWidget(metric)
        page.addLayout(metrics)
        self.workcell_notice = label("等待工作台状态同步", "muted", wrap=True)
        page.addWidget(self.workcell_notice)
        main = QHBoxLayout()
        main.setSpacing(16)
        form = Card()
        form.setFixedWidth(304)
        form.box.addWidget(label("新建订单", "sectionTitle"))
        self.order_id = QLineEdit()
        self.order_id.setPlaceholderText("订单编号")
        self.order_id.setToolTip("订单编号必须唯一")
        self.order_id.textChanged.connect(self._refresh_draft)
        form.box.addWidget(self.order_id)
        self.quantity_spins = {}
        for sku, name in NAMES.items():
            row = QHBoxLayout()
            row.addWidget(ProduceGlyph(sku))
            row.addWidget(label(name))
            row.addStretch()
            spin = QSpinBox()
            spin.setRange(0, int(self.config["stock"]["initial_counts"].get(sku, 4)))
            spin.setFixedWidth(76)
            spin.setValue(1)
            spin.valueChanged.connect(self._refresh_draft)
            self.quantity_spins[sku] = spin
            row.addWidget(spin)
            form.box.addLayout(row)
        self.draft_summary = label("", "sectionTitle")
        self.draft_hint = label("", "muted", wrap=True)
        form.box.addWidget(self.draft_summary)
        form.box.addWidget(self.draft_hint)
        form.box.addStretch()
        self.submit_button = QPushButton("提交订单")
        self.submit_button.setObjectName("primary")
        self.submit_button.clicked.connect(self._submit)
        form.box.addWidget(self.submit_button)
        self.retry_button = QPushButton("换订单号")
        self.retry_button.clicked.connect(self._new_id)
        form.box.addWidget(self.retry_button)
        main.addWidget(form)
        right = QVBoxLayout()
        task = Card()
        task_header = QHBoxLayout()
        self.task_title = label("任务进度", "sectionTitle")
        task_header.addWidget(self.task_title)
        task_header.addStretch()
        self.task_status = label("尚未提交", "muted")
        task_header.addWidget(self.task_status)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setToolTip("取消当前订单：运动到安全检查点后停止")
        self.cancel_button.clicked.connect(self.worker.cancel)
        self.cancel_button.setEnabled(False)
        task_header.addWidget(self.cancel_button)
        task.box.addLayout(task_header)
        self.task_id = label("等待新订单", "muted")
        task.box.addWidget(self.task_id)
        self.steps = StageStrip()
        task.box.addWidget(self.steps)
        task_row = QHBoxLayout()
        self.task_count = label("— / —", "metricValue")
        self.stage = label("准备接单", "muted")
        task_row.addWidget(self.task_count)
        task_row.addStretch()
        task_row.addWidget(self.stage)
        task.box.addLayout(task_row)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        task.box.addWidget(self.progress)
        self.failure_reason = label("", "muted", wrap=True)
        self.failure_reason.hide()
        task.box.addWidget(self.failure_reason)
        right.addWidget(task)
        boxes = Card()
        boxes.box.addWidget(label("订单箱位", "sectionTitle"))
        grid = QGridLayout()
        self.bin_cards = [BinCard(f"A{i+1}") for i in range(4)]
        for i, card in enumerate(self.bin_cards):
            grid.addWidget(card, i // 2, i % 2)
            card.update_resource({}, False, "fulfillment")
        boxes.box.addLayout(grid)
        pickup_row = QHBoxLayout()
        self.pickup_orders = QComboBox()
        self.pickup_orders.setMinimumContentsLength(12)
        self.pickup_orders.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pickup_orders.setToolTip("选择已完成订单，确认人工取走箱子")
        self.pickup_button = QPushButton("确认取货")
        self.pickup_button.clicked.connect(self._pickup)
        pickup_row.addWidget(self.pickup_orders, 1)
        pickup_row.addWidget(self.pickup_button)
        boxes.box.addLayout(pickup_row)
        right.addWidget(boxes)
        main.addLayout(right, 1)
        page.addLayout(main)
        records = Card()
        self.records = records
        self.tabs = QTabWidget()
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(["订单", "商品", "结果", "完成", "取货"])
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history.setSelectionBehavior(QTableWidget.SelectRows)
        self.history.verticalHeader().hide()
        self.history.verticalHeader().setDefaultSectionSize(40)
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.history.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.history.setMinimumHeight(140)
        self.history.setMaximumHeight(180)
        self.events = QPlainTextEdit()
        self.technical = QPlainTextEdit()
        for log in (self.events, self.technical):
            log.setReadOnly(True)
            log.setMaximumBlockCount(500)
            log.setMinimumHeight(140)
            log.setMaximumHeight(180)
        self.tabs.addTab(self.history, "订单记录")
        self.tabs.addTab(self.events, "事件")
        self.tabs.addTab(self.technical, "技术日志")
        records.box.addWidget(self.tabs)
        page.addWidget(records)
        page.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _navigate(self, name, index):
        for button in self.nav_buttons:
            button.setChecked(button.text() == name)
        if name == "工作台":
            self.scroll.verticalScrollBar().setValue(0)
        else:
            self.tabs.setCurrentIndex(index)
            self.scroll.ensureWidgetVisible(self.records)

    def _new_id(self):
        self.order_id.setText(datetime.now().strftime("order-%Y%m%d-%H%M%S-%f"))
        self.order_id.setToolTip(self.order_id.text())

    def _current_order(self):
        return Order(self.order_id.text().strip(), "A", tuple(
            OrderLine(sku, spin.value()) for sku, spin in self.quantity_spins.items() if spin.value()))

    def _refresh_draft(self, _=None):
        order = self._current_order()
        total = sum(line.quantity for line in order.lines)
        self.draft_summary.setText(f"已选 {total} 件 · 1 个订单箱")
        try:
            validate_order(order, self.config)
            self._valid = True
        except ValueError:
            self._valid = False
        limit = self.config["zones"]["A"]["bin_capacity_items"]
        free = next((b.get("bin_id") for b in self._bins if b.get("state") == "FREE"), None)
        hint = f"每单上限 {limit} 件" if not self._valid else (f"预计箱位 {free} · 接单时分配" if free else "等待空闲箱位")
        self.draft_hint.setText(hint)
        self._update_controls()

    def _update_controls(self):
        synced = self._online and monotonic() - self._last_status < 5
        self.submit_button.setEnabled(synced and self._can_submit and self._valid and not self._order_active)
        self.recover_button.setEnabled(synced and self._can_recover and not self._order_active)
        self.pickup_button.setEnabled(synced and self._can_pickup and self.pickup_orders.count() > 0 and not self._order_active)
        self.cancel_button.setEnabled(self._online and self._order_active and self._accepted)
        self.retry_button.setEnabled(not self._order_active)
        self.order_id.setEnabled(not self._order_active)
        for spin in self.quantity_spins.values():
            spin.setEnabled(not self._order_active)

    def _set_connected(self, online):
        self._online = online
        self._check_status()

    def _check_status(self):
        synced = self._online and monotonic() - self._last_status < 5
        self.connection.setText("● 订单服务在线" if synced else "● 状态未同步" if self._online else "● 订单服务离线")
        self.connection.setStyleSheet("color: #377a5c;" if synced else "color: #986d39;")
        self._update_metrics()
        self._update_controls()
        self._render_bins()

    def _set_workcell_status(self, state, reason, can_submit, can_recover):
        self._state, self._can_submit, self._can_recover = state, can_submit, can_recover
        self.workcell_notice.setText(reason if state not in ("READY", "RUNNING") else "")
        self.workcell_notice.setVisible(bool(self.workcell_notice.text()))
        self._update_metrics()
        self._update_controls()

    def _set_resources(self, payload):
        try:
            status = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(status, dict):
            return
        self._last_status = monotonic()
        self._bins = status.get("bins", [])
        self._can_pickup = bool(status.get("can_pickup"))
        selected = self.pickup_orders.currentData()
        self.pickup_orders.clear()
        waiting = {b["order_id"]: b.get("bin_id", "") for b in self._bins if b.get("state") == "WAITING_PICKUP" and b.get("order_id")}
        for order_id, bin_id in waiting.items():
            self.pickup_orders.addItem(f"{bin_id} · …{order_id[-12:]}", order_id)
        index = self.pickup_orders.findData(selected)
        if index >= 0:
            self.pickup_orders.setCurrentIndex(index)
        self._render_bins()
        self._update_metrics()
        self._refresh_draft()
        self._check_status()

    def _render_bins(self):
        by_id = {b.get("bin_id"): b for b in self._bins}
        for i, card in enumerate(self.bin_cards):
            card.update_resource(by_id.get(f"A{i+1}", {}), self._online and monotonic() - self._last_status < 5, self.config.get("operation_mode", "fulfillment"))

    def _update_metrics(self):
        values = [STATE_NAMES.get(self._state, self._state), "1" if self._state == "RUNNING" else "0",
                  str(len({b.get("order_id") for b in self._bins if b.get("state") == "WAITING_PICKUP"})),
                  str(sum(b.get("state") == "FREE" for b in self._bins))]
        for metric, value in zip(self.metrics, values):
            metric.value.setText(value if self._online and monotonic() - self._last_status < 5 else "—")

    def _submit(self):
        if not self.submit_button.isEnabled():
            return
        order = self._current_order()
        validate_order(order, self.config)
        self._order_active, self._accepted = True, False
        self._active_order_id = order.order_id
        self.task_title.setText("当前任务")
        self.task_id.setText(order.order_id)
        self.task_status.setText("提交中")
        self.stage.setText("等待接单")
        self.steps.set_stage(0)
        self._set_progress(0, sum(line.quantity for line in order.lines))
        self.failure_reason.hide()
        self._update_controls()
        self.worker.submit(order.order_id, {line.sku: line.quantity for line in order.lines}, True)

    def _set_accepted(self, accepted):
        self._accepted = accepted
        if not accepted:
            self._order_active = False
            self.task_status.setText("未接收")
            self.stage.setText("请查看事件")
        else:
            self.task_status.setText("执行中")
        self._update_controls()

    def _set_progress(self, completed, total):
        self.task_count.setText(f"{completed} / {total}")
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(completed)

    def _on_feedback(self, stage, completed, total, detail):
        self._set_progress(completed, total)
        try:
            event = json.loads(detail)
        except ValueError:
            event = {}
        tool = event.get("tool", "")
        index = 1 if "PERCEPTION" in stage or tool == "get_object_positions" else 2
        if stage in ("ORDER_STARTED", "ORDER_BIN_ASSIGNED", "ORDER_BIN_ARRIVED"):
            index = 0
        if stage in ("PLACING", "OBJECT_DETACHED", "RETRACTING"):
            index = 3
        phase = event.get("step", "")
        if "place" in str(phase) or "release" in str(phase):
            index = 3
        if stage in ("ITEM_COMPLETED", "VERIFYING", "ORDER_COMPLETED") or tool == "finish_order":
            index = 4
        if stage == "CANCEL_REQUESTED":
            self.stage.setText("等待安全取消")
            self.technical.appendPlainText(f"{stage}\n{detail}")
            return
        self.steps.set_stage(index)
        self.stage.setText(["接单", "视觉定位", "抓取 / 搬运", "装箱", "结果校验"][index])
        self.stage.setToolTip(stage)
        self.technical.appendPlainText(f"{stage}\n{detail}")
        if stage == "AGENT_TOOL_CALL":
            self._append(f"Agent → {tool}")
        elif stage == "ITEM_COMPLETED":
            self._append(f"已装箱 {NAMES.get(event.get('sku'), event.get('sku', ''))} · {completed}/{total}")
        elif stage in ("PERCEPTION_EMPTY", "PERCEPTION_EXHAUSTED"):
            self._append(f"视觉未找到目标 · {event.get('attempts', '')}/{event.get('limit', '')}")

    def _on_result(self, status, completed, requested, error, report):
        self._order_active = self._accepted = False
        self.task_status.setText(STATE_NAMES.get(status, status))
        self.task_title.setText("最近任务")
        self.stage.setText("等待取货" if status == "COMPLETED" else "任务已结束")
        self._set_progress(completed, requested)
        if status == "COMPLETED":
            self.steps.set_stage(4)
        message = readable_error(error) if error else ""
        self.failure_reason.setText(message[:160] + ("…" if len(message) > 160 else ""))
        self.failure_reason.setToolTip(message)
        self.failure_reason.setVisible(bool(error))
        self._append(f"{STATE_NAMES.get(status, status)} · {completed}/{requested}")
        self.technical.appendPlainText(f"结果：{status}\n错误：{error}\n报告：{report}")
        self._new_id()
        self._update_controls()
        self._refresh_history()

    def _recover(self):
        if not self.recover_button.isEnabled():
            return
        if QMessageBox.question(self, "恢复工作台", "清理失败订单和桌面，重新准备库存？\n待取货订单必须先取走。") == QMessageBox.Yes:
            self.worker.recover()

    def _pickup(self):
        if not self.pickup_button.isEnabled():
            return
        order_id = self.pickup_orders.currentData()
        if QMessageBox.question(self, "确认取货", f"取走订单 {order_id}？\n对应箱子和商品将从仿真中移除。") == QMessageBox.Yes:
            self.worker.pickup(order_id)

    def _append(self, message):
        self.events.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def _restore_last_task(self):
        """A panel restart must not hide the last persisted order result."""
        if self.history.rowCount() == 0:
            return
        path = Path(self.history.item(0, 0).toolTip())
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            status = result.get("status", "UNKNOWN")
            self.task_title.setText("最近任务")
            self.task_id.setText(result.get("order_id", path.stem))
            self.task_status.setText(STATE_NAMES.get(status, status))
            self._set_progress(int(result.get("completed", 0)), int(result.get("requested", 0)))
            self.stage.setText("历史结果")
            error = result.get("error", "")
            message = readable_error(error) if error else ""
            self.failure_reason.setText(message[:160] + ("…" if len(message) > 160 else ""))
            self.failure_reason.setToolTip(message)
            self.failure_reason.setVisible(bool(error))
        except (OSError, ValueError, TypeError):
            return

    def _refresh_history(self):
        paths = sorted(self.report_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if self.report_dir.exists() else []
        rows = []
        for path in paths:
            if path.name.endswith(".pickup.json"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                order_id = data.get("order_id", path.stem)
                lines = data.get("lines", [])
                products = " / ".join(f"{NAMES.get(l.get('sku'), l.get('sku', ''))}×{l.get('quantity', '')}" for l in lines)
                delivery = "已取货" if path.with_name(f"{path.stem}.pickup.json").exists() else "—"
                if any(b.get("order_id") == order_id and b.get("state") == "WAITING_PICKUP" for b in self._bins):
                    delivery = "待取货"
                rows.append((path, [order_id, products or "—", STATE_NAMES.get(data.get("status"), data.get("status", "—")), f"{data.get('completed', 0)}/{data.get('requested', 0)}", delivery]))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            if len(rows) >= 50:
                break
        self.history.setRowCount(len(rows))
        for row, (path, values) in enumerate(rows):
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(path) if col == 0 else str(value))
                self.history.setItem(row, col, item)
        self.tabs.setTabText(0, f"订单记录 · {len(rows)}")

    def closeEvent(self, event):
        if self._started:
            self.worker.stop()
            self.worker.wait(3000)
        super().closeEvent(event)
