"""Operator presentation only: ROS transport stays in debug_panel.RosWorker."""

from datetime import datetime
import json
from pathlib import Path
from time import monotonic

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLineEdit, QMenu, QToolButton,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
import yaml

from .order import Order, OrderLine, validate_order, readable_error
from .operator_incident import describe_incident, resolution_steps
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
        self._last_inventory = 0.0
        self._order_active = False
        self._accepted = False
        self._active_order_id = ""
        self._can_submit = self._can_recover = self._can_pickup = False
        self._valid = False
        self._state = "UNKNOWN"
        self._state_reason = ""
        self._scan_request_pending = False
        self._scan_requested_at = 0.0
        self._incident_order_id = None
        self._camera_checking = False
        self._camera_check_started = 0.0
        self._bins = []
        self._queue_enabled = False
        self._queue_clear_pending = False
        self._cancel_requesting = False
        self._cancel_waiting = False
        self._cancel_for_id = ""
        self._can_register = False
        self._registration_pending = False
        self._registration_order_id = None
        self._registration_reconcile_allowed = False
        self._submitted_order_id = None
        self._last_queue_terminal = None
        self._started = start_worker
        self.worker = worker_factory()
        self.setWindowTitle("生鲜配货 · ROS 2 工作台")
        self.setObjectName("operatorPanel")
        self.resize(1280, 900)
        self.setMinimumSize(1000, 700)
        self.setStyleSheet(STYLE)
        self._build_ui()
        if hasattr(self.worker, "queue_cleared"):
            self.worker.queue_cleared.connect(self._on_queue_cleared)
        if hasattr(self.worker, "operation_finished"):
            self.worker.operation_finished.connect(self._on_operation_finished)
        if hasattr(self.worker, "camera_diagnostic"):
            self.worker.camera_diagnostic.connect(self._on_camera_diagnostic)
        self._new_id()
        self._refresh_history()
        self._restore_last_task()
        self._refresh_draft()
        for signal, slot in (
            (self.worker.connected, self._set_connected),
            (self.worker.accepted, self._set_accepted),
            (self.worker.feedback, self._on_feedback),
            (self.worker.finished, self._on_result),
            (self.worker.notice, self._on_notice),
            (self.worker.workcell_status, self._set_workcell_status),
            (self.worker.resources, self._set_resources),
            (self.worker.inventory, self._set_inventory),
        ):
            signal.connect(slot)
        if hasattr(self.worker, "registered"):
            self.worker.registered.connect(self._on_registered)
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
        self.nav_buttons = []
        # Internal safety gate; the visible recovery entry is in the task footer.
        self.recover_button = QPushButton("恢复工作台", self)
        self.recover_button.hide()
        self.recover_button.clicked.connect(self._recover)
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
        self.operation_notice = label("", "muted", wrap=True)
        self.operation_notice.hide()
        page.addWidget(self.operation_notice)
        self.incident_card = QWidget()
        self.incident_card.setObjectName("taskSupport")
        self.incident_card.box = QVBoxLayout(self.incident_card)
        self.incident_card.box.setContentsMargins(0, 0, 0, 0)
        self.incident_title = label("", "sectionTitle")
        self.incident_reason = label("", "muted", wrap=True)
        self.incident_impact = label("", "muted", wrap=True)
        self.incident_next = label("", "muted", wrap=True)
        self.camera_result = label("", "muted", wrap=True)
        self.camera_result.hide()
        for widget in (self.incident_title, self.incident_reason, self.incident_impact, self.incident_next):
            widget.setParent(self.incident_card)
            widget.hide()
        self.incident_card.box.addWidget(self.camera_result)
        actions = QHBoxLayout()
        self.incident_camera = QPushButton("检查相机数据")
        self.incident_camera.clicked.connect(self._check_camera)
        self.incident_scan = QPushButton("重试库存复查")
        self.incident_scan.clicked.connect(self._scan_inventory)
        self.incident_cancel = QPushButton("取消此等待订单")
        self.incident_cancel.clicked.connect(self._cancel_incident_order)
        self.incident_recover = QPushButton("恢复工作台…")
        self.incident_recover.clicked.connect(self._recover)
        self.incident_logs = QPushButton("查看技术日志")
        self.incident_logs.clicked.connect(lambda: self._navigate("运行日志", 2))
        self.incident_guide = QPushButton("处理指南")
        self.incident_guide.clicked.connect(self._show_resolution_guide)
        self.help_menu_button = QToolButton()
        self.help_menu_button.setText("诊断与帮助")
        self.help_menu_button.setStyleSheet("QToolButton { background: white; border: 1px solid #dbe5df; border-radius: 6px; padding: 8px 12px; }")
        self.help_menu_button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self.help_menu_button)
        self.camera_action = menu.addAction("检查相机数据", self.incident_camera.click)
        menu.addAction("处理指南", self._show_resolution_guide)
        menu.addAction("查看技术日志", lambda: self._navigate("运行日志", 2))
        self.recovery_action = menu.addAction("恢复 / 重置工作台…", self._recover)
        self.help_menu_button.setMenu(menu)
        for button in (self.incident_camera, self.incident_guide, self.incident_logs):
            button.setParent(self.incident_card)
            button.hide()
        for button in (self.incident_scan, self.incident_cancel, self.incident_recover, self.help_menu_button):
            actions.addWidget(button)
        actions.addStretch()
        self.incident_card.box.addLayout(actions)
        self.incident_card.hide()
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
        self.priority = QSpinBox()
        self.priority.setRange(0, 10)
        self.priority.setPrefix("优先级 ")
        self.priority.setToolTip("数值越大越优先；不会中途抢占正在搬运的商品")
        form.box.addWidget(self.priority)
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
        self.cancel_button.clicked.connect(self._request_cancel)
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
        task.box.addWidget(self.incident_card)
        right.addWidget(task)
        boxes = Card()
        self.boxes_card = boxes
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
        self.clear_logs_button = QPushButton("清空日志")
        self.clear_logs_button.setToolTip("仅清空事件和技术日志显示，保留订单报告及系统日志")
        self.clear_logs_button.clicked.connect(self._clear_logs)
        self.tabs.setCornerWidget(self.clear_logs_button)
        self.tabs.currentChanged.connect(lambda index: self.clear_logs_button.setVisible(index in (1, 2)))
        self.clear_logs_button.hide()
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
        inventory_page = QWidget()
        inventory_layout = QVBoxLayout(inventory_page)
        inventory_layout.setContentsMargins(0, 6, 0, 0)
        self.inventory_hint = label("等待视觉台账同步 · 候选数量不等于实物库存", "muted")
        inventory_layout.addWidget(self.inventory_hint)
        self.inventory_table = QTableWidget(0, 6)
        self.inventory_table.setHorizontalHeaderLabels(["候选 ID", "类别（未确认）", "状态", "观测位置 / m", "时效", "订单"])
        self.inventory_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.inventory_table.verticalHeader().hide()
        self.inventory_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.inventory_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.inventory_table.setMinimumHeight(140)
        self.inventory_table.setMaximumHeight(180)
        inventory_layout.addWidget(self.inventory_table)
        self.tabs.addTab(inventory_page, "视觉台账")
        queue_page = QWidget()
        queue_layout = QVBoxLayout(queue_page)
        queue_layout.setContentsMargins(0, 6, 0, 0)
        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(["订单", "优先级", "状态", "进度", "等待原因"])
        self.queue_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.queue_table.itemSelectionChanged.connect(self._update_controls)
        self.queue_table.verticalHeader().hide()
        self.queue_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.queue_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.queue_table.setMinimumHeight(140)
        self.queue_table.setMaximumHeight(180)
        queue_layout.addWidget(self.queue_table)
        actions = QHBoxLayout()
        self.queue_cancel_button = QPushButton("取消选中排队单")
        self.queue_cancel_button.clicked.connect(self._cancel_queued)
        self.queue_clear_button = QPushButton("清除待执行队列")
        self.queue_clear_button.setToolTip("保留正在执行、已部分装箱及待取货的订单")
        self.queue_clear_button.clicked.connect(self._clear_queue)
        self.scan_button = QPushButton("视觉复查")
        self.scan_button.clicked.connect(self._scan_inventory)
        self.replenish_button = QPushButton("补货准备")
        self.replenish_button.clicked.connect(self._replenish)
        for button in (self.queue_cancel_button, self.queue_clear_button, self.scan_button, self.replenish_button):
            actions.addWidget(button)
        queue_layout.addLayout(actions)
        self.tabs.addTab(queue_page, "任务调度")
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
        if self._valid and self._state == "RECOVERY_REQUIRED":
            hint = "先处理失败现场；新订单只登记、暂不执行"
        self.draft_hint.setText(hint)
        self._update_controls()

    def _update_controls(self):
        synced = self._online and monotonic() - self._last_status < 5
        self.submit_button.setEnabled(synced and self._valid and (
            self._can_register and not self._registration_pending if self._queue_enabled
            else self._can_submit and not self._order_active))
        self.submit_button.setText("提交中…" if self._registration_pending else "提交订单")
        waiting_pickup = any(b.get("state") == "WAITING_PICKUP" for b in self._bins)
        paused = any(b.get("state") == "PAUSED" for b in self._bins)
        self.recover_button.setEnabled(synced and not self._order_active
            and (self._can_recover or (self._state == "RECOVERY_REQUIRED" and (waiting_pickup or paused))))
        self.recovery_action.setEnabled(self.recover_button.isEnabled())
        if self._state == "RECOVERY_REQUIRED":
            if not self._can_recover and waiting_pickup:
                ids = ', '.join(b['bin_id'] for b in self._bins if b.get('state') == 'WAITING_PICKUP')
                self.workcell_notice.setText(f"现场需要恢复；先确认取走 {ids} 的已完成订单，再恢复失败现场。")
            elif self._can_recover:
                self.workcell_notice.setText("失败现场需核对；确认后可恢复工作台，将清理失败箱和桌面。")
        self.pickup_button.setEnabled(synced and self._can_pickup and self.pickup_orders.count() > 0 and not self._order_active)
        self.cancel_button.setEnabled(synced and self._order_active and self._accepted
            and not self._cancel_requesting and not self._cancel_waiting)
        self.cancel_button.setText("请求取消中…" if self._cancel_requesting else
                                   "等待安全取消" if self._cancel_waiting else "取消")
        editable = not self._registration_pending if self._queue_enabled else not self._order_active
        self.retry_button.setEnabled(editable)
        self.order_id.setEnabled(editable)
        self.priority.setEnabled(editable)
        for spin in self.quantity_spins.values():
            spin.setEnabled(editable)
        self.scan_button.setEnabled(synced and self._queue_enabled
            and not self._scan_request_pending and self._state != "SCANNING")
        row = self.queue_table.currentRow()
        cell = self.queue_table.item(row, 0) if row >= 0 else None
        job = getattr(self, "_queue_jobs", {}).get(cell.text(), {}) if cell else {}
        self.queue_cancel_button.setEnabled(synced and self._queue_enabled
            and job.get("state") in ("QUEUED", "WAITING_STOCK", "WAITING_BIN", "PAUSED"))
        self.queue_clear_button.setEnabled(synced and self._queue_enabled
            and not self._queue_clear_pending and bool(self._clearable_orders()))
        self.replenish_button.setEnabled(synced and self._queue_enabled and self._state == "READY")
        self._refresh_incident(synced)

    def _refresh_incident(self, synced):
        job = getattr(self, "_queue_jobs", {}).get(self.task_id.text())
        incident = describe_incident(self._state, self._state_reason, synced, job,
                                     self.recover_button.isEnabled())
        self.incident_card.setVisible(incident is not None)
        if incident is None:
            self._incident_order_id = None
            return
        self._incident_order_id = job.get("order_id") if job else None
        self.incident_title.setText(incident.title)
        self.incident_reason.setText("原因：" + (incident.reason[:180] or "等待进一步核对"))
        self.incident_reason.setToolTip(incident.reason)
        self.incident_impact.setText("影响：" + incident.impact)
        self.incident_next.setText("下一步：" + incident.next_step)
        self.incident_scan.setVisible(incident.scan)
        self.incident_scan.setEnabled(self.scan_button.isEnabled())
        self.incident_cancel.setVisible(incident.cancel_waiting)
        self.incident_cancel.setEnabled(synced and incident.cancel_waiting)
        self.incident_recover.setVisible(self._state == "RECOVERY_REQUIRED")
        self.incident_recover.setEnabled(incident.recover)
        waiting_pickup = any(b.get("state") == "WAITING_PICKUP" for b in self._bins)
        self.incident_recover.setText("先确认取货" if not self._can_recover and waiting_pickup
            else "处理暂停订单" if not self._can_recover and any(b.get("state") == "PAUSED" for b in self._bins)
            else "恢复工作台…")
        self.incident_logs.setEnabled(True)
        self.incident_camera.hide()
        self.incident_camera.setEnabled(synced and not self._camera_checking and hasattr(self.worker, "check_camera"))
        self.camera_action.setEnabled(self.incident_camera.isEnabled())
        self.incident_camera.setText("检查中…" if self._camera_checking else "检查相机数据")

    def _show_resolution_guide(self):
        job = getattr(self, "_queue_jobs", {}).get(self._incident_order_id)
        dialog = QDialog(self)
        dialog.setWindowTitle("问题处理指南")
        dialog.setMinimumWidth(600)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(self.incident_title.text(), "sectionTitle"))
        layout.addWidget(label(self.incident_reason.text(), "muted", wrap=True))
        layout.addWidget(label(self.incident_impact.text(), "muted", wrap=True))
        for index, step in enumerate(resolution_steps(self._state, job), 1):
            layout.addWidget(label(f"{index}. {step}", "muted", wrap=True))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec_()

    def _check_camera(self):
        if not self.incident_camera.isEnabled():
            return
        self._camera_checking = True
        self._camera_check_started = monotonic()
        self.camera_result.setText("正在检查三路相机消息，约 2 秒；不运行视觉模型、不移动机械臂。")
        self.camera_result.show()
        self.worker.check_camera(self.config["camera"])
        self._update_controls()

    def _on_camera_diagnostic(self, payload):
        try:
            result = json.loads(payload)
        except (ValueError, TypeError):
            return
        self._camera_checking = False
        names = {"color": "图像", "depth": "深度", "info": "相机参数"}
        summary = " / ".join(f"{names.get(row['kind'], row['kind'])}：{'收到' if row['messages'] else '未收到'}"
                             for row in result["topics"])
        next_step = "消息链路有数据，可重试库存复查；不代表识别、时间同步或三维坐标已正确。" if result["all_received"] else "先检查仿真是否暂停或退出，恢复后重新检查；不要重复下单。"
        self.camera_result.setText(f"{datetime.now():%H:%M:%S} 检查 · " + summary + "。" + next_step)
        self.camera_result.setToolTip(payload)
        self.camera_result.show()
        self._on_notice(summary + "。" + next_step)
        self._update_controls()

    def _cancel_incident_order(self):
        identifier = self._incident_order_id
        job = getattr(self, "_queue_jobs", {}).get(identifier, {})
        if not self.incident_cancel.isEnabled() or job.get("state") not in (
                "QUEUED", "WAITING_STOCK", "WAITING_BIN") or job.get("completed", 0):
            self._on_notice("订单状态已变化，请核对当前任务；未发送取消请求")
            return
        if QMessageBox.question(self, "取消等待订单", f"取消订单 {identifier}？\n不会清除其他订单或移动商品。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._on_notice(f"正在请求取消等待订单 {identifier}…")
            self.worker.cancel_queued(identifier)

    def _set_connected(self, online):
        self._online = online
        self._check_status()

    def _check_status(self):
        if self._camera_checking and monotonic() - self._camera_check_started > 10:
            self._camera_checking = False
            self.camera_result.setText("相机检查响应未确认，请先检查通信；不能据此认定相机故障。")
        if self._scan_request_pending and monotonic() - self._scan_requested_at > 10:
            self._scan_request_pending = False
            self._on_notice("复查请求状态尚未确认；请核对工作台状态，勿连续点击")
        synced = self._online and monotonic() - self._last_status < 5
        if self._last_inventory and (not synced or monotonic() - self._last_inventory >= 5):
            self.inventory_hint.setText("历史视觉观测 · 台账未同步 · 不可当作当前实物库存")
        self.connection.setText("● 订单服务在线" if synced else "● 状态未同步" if self._online else "● 订单服务离线")
        self.connection.setStyleSheet("color: #377a5c;" if synced else "color: #986d39;")
        self._update_metrics()
        self._update_controls()
        self._render_bins()

    def _set_workcell_status(self, state, reason, can_submit, can_recover):
        # Compatibility with an older server that labels a failed scan READY.
        if state == "READY" and reason.startswith("视觉库存复查失败"):
            state = "INVENTORY_ERROR"
        if state != self._state:
            self.camera_result.hide()
        self._state, self._can_submit, self._can_recover = state, can_submit, can_recover
        self._state_reason = reason
        if state == "SCANNING":
            self._scan_request_pending = False
        self.workcell_notice.setText("库存复查失败 · 订单保留，处理步骤见下方" if state == "INVENTORY_ERROR"
            else reason if state not in ("READY", "RUNNING") else "")
        self.workcell_notice.setToolTip(reason)
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
        self._queue_enabled = bool(status.get("queue_enabled"))
        self._can_register = bool(status.get("can_register"))
        if self._registration_pending and self._registration_reconcile_allowed and any(job.get("order_id") == self._registration_order_id
                for job in status.get("orders", [])):
            self._on_registered(True, self._registration_order_id, "状态台账确认订单已登记")
        self._render_queue(status.get("orders", []))
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

    def _render_queue(self, orders):
        row = self.queue_table.currentRow()
        selected_cell = self.queue_table.item(row, 0) if row >= 0 else None
        selected_id = selected_cell.text() if selected_cell else None
        self._queue_jobs = {job["order_id"]: job for job in orders}
        names = {"QUEUED": "排队", "DISPATCHING": "资源已预留", "RUNNING": "执行中",
                 "WAITING_STOCK": "等待库存核对", "WAITING_BIN": "等待箱位",
                 "WAITING_PICKUP": "等待取货", "COLLECTED": "已取货", "FAILED": "失败",
                 "CANCELED": "已取消", "CANCELLED": "已取消", "PAUSED": "暂停 · 保留箱位",
                 "EXECUTION_UNCERTAIN": "执行结果不明 · 待核对"}
        visible_orders = [job for job in orders if job["state"] not in ("CANCELED", "CANCELLED")]
        self.queue_table.setRowCount(len(visible_orders))
        for row, job in enumerate(visible_orders):
            reason = job.get("reason", "")
            if self._state == "RECOVERY_REQUIRED" and job["state"] in ("QUEUED", "WAITING_STOCK", "WAITING_BIN"):
                reason = "现场需要恢复，尚未执行 · " + reason
            if job.get("missing"):
                reason += " · " + " / ".join(f"{NAMES.get(sku, sku)}缺少可用观测 {count}"
                                                for sku, count in job["missing"].items())
            values = [job["order_id"], job["priority"], names.get(job["state"], job["state"]),
                      f"{job['completed']}/{job['total']}", reason]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.queue_table.setItem(row, col, cell)
            if job["order_id"] == selected_id:
                self.queue_table.selectRow(row)
        if not self._queue_enabled:
            return
        running = next((job for job in orders if job["state"] in ("RUNNING", "DISPATCHING", "EXECUTION_UNCERTAIN")), None)
        self._order_active = self._accepted = running is not None
        if running:
            if self._cancel_for_id != running["order_id"]:
                self._cancel_requesting = self._cancel_waiting = False
            if self.task_id.text() != running["order_id"]:
                self.failure_reason.clear()
                self.failure_reason.hide()
            self.task_id.setText(running["order_id"])
            self.task_status.setText(names[running["state"]])
            self._set_progress(running["completed"], running["total"])
        else:
            self._cancel_requesting = self._cancel_waiting = False
            waiting = [job for job in orders if job["state"] in (
                "QUEUED", "WAITING_STOCK", "WAITING_BIN", "PAUSED")]
            focus = next((job for job in waiting if job["order_id"] == self._submitted_order_id),
                         waiting[0] if waiting else None)
            if self._registration_pending:
                return
            if focus:
                self.task_id.setText(focus["order_id"])
                self.task_status.setText(names[focus["state"]])
                self.stage.setText(names[focus["state"]])
                self.steps.set_stage(0)
                self._set_progress(focus["completed"], focus["total"])
                self.failure_reason.setText(focus.get("reason", ""))
                self.failure_reason.setVisible(bool(focus.get("reason")))
                self._last_queue_terminal = None
                return
            terminal = max((job for job in orders if job["state"] in (
                "WAITING_PICKUP", "FAILED", "CANCELLED")),
                key=lambda job: job.get("finished_sequence", 0), default=None)
            if terminal and self._last_queue_terminal != (terminal["order_id"], terminal["state"]):
                self._last_queue_terminal = (terminal["order_id"], terminal["state"])
                self.task_id.setText(terminal["order_id"])
                self.task_status.setText(names[terminal["state"]])
                self._set_progress(terminal["completed"], terminal["total"])
                self.failure_reason.setText(terminal["reason"])
                self.failure_reason.setVisible(terminal["state"] != "WAITING_PICKUP")

    def _clearable_orders(self):
        return [identifier for identifier, job in getattr(self, "_queue_jobs", {}).items()
                if job["state"] in ("QUEUED", "WAITING_STOCK", "WAITING_BIN")
                and not job.get("completed", 0)]

    def _clear_queue(self):
        ids = self._clearable_orders()
        if not ids or self._queue_clear_pending or not self.queue_clear_button.isEnabled():
            return
        if QMessageBox.question(self, "清除待执行队列",
                f"取消当前 {len(ids)} 份尚未执行的订单？\n正在执行、已部分装箱和待取货订单将保留。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._queue_clear_pending = True
        self.queue_clear_button.setEnabled(False)
        self._on_notice(f"正在清除 {len(ids)} 份待执行订单，等待服务确认…")
        self.worker.clear_queue(ids)

    def _on_queue_cleared(self, cleared, skipped):
        self._queue_clear_pending = False
        self._on_notice(f"队列清理：已清除 {cleared} 份，跳过或未确认 {skipped} 份")
        self._check_status()

    def _clear_logs(self):
        self.events.clear()
        self.technical.clear()

    def _cancel_queued(self):
        row = self.queue_table.currentRow()
        if row < 0:
            self._on_notice("请先选中一份待执行或安全暂停的订单")
            return
        if row >= 0 and self._queue_enabled:
            order_id = self.queue_table.item(row, 0).text()
            job = self._queue_jobs[order_id]
            if job["completed"]:
                if QMessageBox.question(self, "丢弃部分订单", "仅允许丢弃安全暂停的订单。确认移走已装商品和订单箱？") == QMessageBox.Yes:
                    self._on_notice("已请求丢弃安全暂停订单，等待服务确认…")
                    self.worker.cancel_queued(order_id, True)
            else:
                self._on_notice("正在取消选中订单，等待服务确认…")
                self.worker.cancel_queued(order_id)

    def _request_cancel(self):
        if not self.cancel_button.isEnabled():
            return
        self._cancel_for_id = self.task_id.text()
        self._cancel_requesting = True
        self._on_notice("正在发送取消请求；感知等待可取消，抓放运动须等待安全检查点")
        self._update_controls()
        self.worker.cancel()

    def _on_operation_finished(self, operation, success, message):
        if operation == "cancel":
            self._cancel_requesting = False
            self._cancel_waiting = success
        if operation == "refresh_inventory" and not success:
            self._scan_request_pending = False
        self._on_notice(message)
        self._update_controls()

    def _on_notice(self, message):
        self.operation_notice.setText(message)
        self.operation_notice.show()
        self._append(message)

    def _replenish(self):
        if QMessageBox.question(self, "补货准备", "替换桌面剩余货源？已完成的订单箱将保留。") == QMessageBox.Yes:
            self._on_notice("已请求补货准备，等待服务确认…")
            self.worker.replenish()

    def _scan_inventory(self):
        if not self.scan_button.isEnabled():
            return
        self._scan_request_pending = True
        self._scan_requested_at = monotonic()
        self._on_notice("已请求视觉复查，将在工作台空闲时扫描")
        self.worker.refresh_inventory()
        self._update_controls()

    def _on_registered(self, success, order_id, message):
        if not self._registration_pending or order_id != self._registration_order_id:
            return
        self._registration_pending = False
        self._registration_order_id = None
        self._on_notice(f"{order_id} · {message}")
        if success:
            self._submitted_order_id = order_id
            self._new_id()
            if not self._order_active:
                self.task_status.setText("已登记 · 等待调度")
                self.stage.setText("等待调度")
        elif not self._order_active:
            self.task_status.setText("未确认" if "未确认" in message else "未接收")
            self.failure_reason.setText(message)
            self.failure_reason.show()
        self._update_controls()

    def _set_inventory(self, payload):
        try:
            snapshot = json.loads(payload)
        except (ValueError, TypeError):
            return
        self._last_inventory = monotonic()
        states = {"AVAILABLE": "可选择", "RESERVED": "已预留", "CARRYING": "搬运中",
                  "PLACED": "已装箱", "COLLECTED": "已取货", "UNKNOWN": "未知"}
        items = snapshot.get("items", [])
        self.inventory_hint.setText(f"已观测 {len(items)} 个候选 · 场景批次 {snapshot.get('epoch', 0)} · 非实物库存保证")
        self.inventory_table.setRowCount(len(items))
        for row, item in enumerate(items):
            point = item.get("position", [0, 0, 0])
            values = [item.get("item_id", ""), NAMES.get(item.get("sku"), item.get("sku", "")),
                      states.get(item.get("state"), item.get("state", "")),
                      " / ".join(f"{v:.3f}" for v in point),
                      "过期" if item.get("stale") else f"{item.get('age_sec', 0):.0f}s",
                      item.get("order_id", "") or "—"]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(f"{value}\n坐标系：{item.get('frame_id', '')}")
                self.inventory_table.setItem(row, col, cell)

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
        if self._queue_enabled:
            self._registration_pending = True
            self._registration_order_id = order.order_id
            self._registration_reconcile_allowed = order.order_id not in getattr(self, "_queue_jobs", {})
            self._on_notice(f"正在提交 {order.order_id}，等待登记确认…")
            if not self._order_active:
                self.task_id.setText(order.order_id)
                self.task_status.setText("提交中")
                self.stage.setText("等待登记确认")
                self.steps.set_stage(0)
                self._set_progress(0, order.total_items)
                self.failure_reason.clear()
                self.failure_reason.hide()
            self._update_controls()
            self.worker.submit(order.order_id, {line.sku: line.quantity for line in order.lines},
                               True, self.priority.value())
            return
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
        if not self._can_recover:
            waiting = [b.get("bin_id") for b in self._bins if b.get("state") == "WAITING_PICKUP"]
            if waiting:
                self._on_notice(f"恢复前请先取走已完成订单箱 {', '.join(waiting)}；在下方选择订单并确认取货。失败箱会保留，未发送重置请求。")
                self.scroll.ensureWidgetVisible(self.boxes_card)
                self.pickup_orders.setFocus()
            else:
                self._on_notice("恢复前请先核对并处理暂停订单；未发送重置请求。")
                self._navigate("任务调度", 4)
            return
        if QMessageBox.question(self, "恢复工作台", "清理失败订单和桌面，重新准备库存？\n待取货订单必须先取走。") == QMessageBox.Yes:
            self._on_notice("已请求恢复工作台，等待服务确认…")
            self.worker.recover()

    def _pickup(self):
        if not self.pickup_button.isEnabled():
            return
        order_id = self.pickup_orders.currentData()
        if QMessageBox.question(self, "确认取货", f"取走订单 {order_id}？\n对应箱子和商品将从仿真中移除。") == QMessageBox.Yes:
            self._on_notice("已请求确认取货，等待服务确认…")
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
            discarded = path.with_suffix(".discard.json").exists()
            if discarded:
                status = "CANCELED"
            self.task_title.setText("最近任务")
            self.task_id.setText(result.get("order_id", path.stem))
            self.task_status.setText(STATE_NAMES.get(status, status))
            self._set_progress(int(result.get("completed", 0)), int(result.get("requested", 0)))
            self.stage.setText("历史结果")
            error = "" if discarded else result.get("error", "")
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
            if path.name.endswith((".pickup.json", ".discard.json")):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                order_id = data.get("order_id", path.stem)
                lines = data.get("lines", [])
                products = " / ".join(f"{NAMES.get(l.get('sku'), l.get('sku', ''))}×{l.get('quantity', '')}" for l in lines)
                delivery = "已取货" if path.with_name(f"{path.stem}.pickup.json").exists() else "—"
                discarded = path.with_suffix(".discard.json").exists()
                if discarded:
                    delivery = "已丢弃"
                if any(b.get("order_id") == order_id and b.get("state") == "WAITING_PICKUP" for b in self._bins):
                    delivery = "待取货"
                status = "CANCELED" if discarded else data.get("status", "—")
                rows.append((path, [order_id, products or "—", STATE_NAMES.get(status, status), f"{data.get('completed', 0)}/{data.get('requested', 0)}", delivery]))
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
