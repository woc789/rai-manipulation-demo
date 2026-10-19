"""Presentation regressions. No ROS calls or simulated orders are sent."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt5.QtCore import QObject, pyqtSignal
    from PyQt5.QtWidgets import QApplication, QPushButton, QScrollArea
except ImportError:
    raise unittest.SkipTest("Qt tests use /usr/bin/python3 with installed PyQt5")
import yaml
from produce_order_workstation.operator_panel import OperatorPanel


class FakeWorker(QObject):
    connected = pyqtSignal(bool)
    accepted = pyqtSignal(bool)
    feedback = pyqtSignal(str, int, int, str)
    finished = pyqtSignal(str, int, int, str, str)
    notice = pyqtSignal(str)
    workcell_status = pyqtSignal(str, str, bool, bool)
    resources = pyqtSignal(str)
    inventory = pyqtSignal(str)
    queue_cleared = pyqtSignal(int, int)
    operation_finished = pyqtSignal(str, bool, str)
    camera_diagnostic = pyqtSignal(str)

    def cancel(self):
        self.cancel_requested = True

    def submit(self, *args):
        self.submission = args

    def cancel_queued(self, order_id):
        self.canceled_order = order_id

    def clear_queue(self, order_ids):
        self.cleared_ids = order_ids

    def refresh_inventory(self):
        self.scan_requested = True

    def check_camera(self, camera):
        self.camera_checked = camera

    def recover(self):
        self.recovery_requested = True


class OperatorPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.config = yaml.safe_load(
            (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.panel = OperatorPanel(
            FakeWorker,
            start_worker=False,
            config=self.config,
            report_dir=Path(self.temp.name),
        )
        self.bins = [
            {"bin_id": f"A{i + 1}", "state": "FREE", "order_id": ""} for i in range(4)
        ]

    def tearDown(self):
        self.panel.close()
        self.temp.cleanup()

    def ready(self):
        self.panel._set_connected(True)
        self.panel._set_workcell_status("READY", "", True, True)
        self.panel._set_resources(json.dumps({"bins": self.bins, "can_pickup": True}))

    def test_initial_and_offline_are_safe(self):
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.ready()
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.panel._set_connected(False)
        for button in (
            self.panel.submit_button,
            self.panel.pickup_button,
            self.panel.recover_button,
        ):
            self.assertFalse(button.isEnabled())

    def test_rejected_maintenance_can_be_confirmed_again_with_visible_state(self):
        from PyQt5.QtWidgets import QMessageBox

        body = dict(
            request_id="retry",
            kind="request_replenishment",
            state="APPROVED",
            execution_state="REJECTED",
        )
        self.panel._set_resources(
            json.dumps({"bins": self.bins, "approval_requests": [body]})
        )
        self.assertIn("可重试", self.panel.business_requests.item(0, 2).text())
        self.panel.business_requests.selectRow(0)
        with (
            patch.object(QMessageBox, "question", return_value=QMessageBox.Yes),
            patch.object(self.panel, "_send_business") as send,
        ):
            self.panel._resolve_business(True)
            send.assert_called_once_with("approve_request", {"request_id": "retry"})

    def test_running_maintenance_cannot_be_confirmed_twice(self):
        body = dict(
            request_id="busy",
            kind="request_replenishment",
            state="APPROVED",
            execution_state="RUNNING",
        )
        self.panel._set_resources(
            json.dumps({"bins": self.bins, "approval_requests": [body]})
        )
        self.assertIn("执行中", self.panel.business_requests.item(0, 2).text())
        self.panel.business_requests.selectRow(0)
        with patch.object(self.panel, "_send_business") as send:
            self.panel._resolve_business(True)
            send.assert_not_called()

    def test_capacity_is_inline_and_does_not_change_task(self):
        self.ready()
        self.panel._on_result("COMPLETED", 4, 4, "", "")
        self.panel.quantity_spins["corn"].setValue(8)
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.assertEqual(self.panel.task_count.text(), "4 / 4")
        self.assertEqual(self.panel.progress.value(), 4)
        self.assertEqual(self.panel.task_status.text(), "配货完成")

    def test_eight_item_mixed_draft_is_allowed(self):
        self.ready()
        for spin in self.panel.quantity_spins.values():
            self.assertEqual(spin.maximum(), 8)
            spin.setValue(2)
        self.assertTrue(self.panel.submit_button.isEnabled())

    def test_heartbeat_preserves_finished_task(self):
        self.ready()
        self.panel._on_result("FAILED", 2, 4, "No object", "")
        self.ready()
        self.assertEqual(self.panel.task_status.text(), "失败")
        self.assertEqual(self.panel.task_count.text(), "2 / 4")

    def test_resources_and_stale_status(self):
        self.bins[0].update(state="WAITING_PICKUP", order_id="order-one")
        self.ready()
        self.assertEqual(self.panel.pickup_orders.currentData(), "order-one")
        self.assertTrue(self.panel.pickup_button.isEnabled())
        self.assertEqual(self.panel.metrics[2].value.text(), "1")
        self.panel._last_status = 0
        self.panel._check_status()
        self.assertFalse(self.panel.pickup_button.isEnabled())
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.assertEqual(self.panel.metrics[3].value.text(), "—")

    def test_motion_feedback_is_not_fake_timer_progress(self):
        self.panel._on_feedback("MOTION_STAGE", 1, 4, json.dumps({"step": "place"}))
        self.assertEqual(self.panel.stage.text(), "装箱")
        self.assertEqual(self.panel.progress.value(), 1)

    def test_submission_is_guarded_and_draft_is_locked(self):
        self.panel._submit()
        self.assertFalse(hasattr(self.panel.worker, "submission"))
        self.ready()
        self.panel._submit()
        self.assertEqual(self.panel.worker.submission[1]["corn"], 1)
        self.assertFalse(self.panel.quantity_spins["corn"].isEnabled())
        self.assertFalse(self.panel.cancel_button.isEnabled())
        self.panel._set_accepted(True)
        self.assertTrue(self.panel.cancel_button.isEnabled())

    def test_history_uses_database_projection_even_if_report_disagrees(self):
        path = Path(self.temp.name) / "test-order.json"
        path.write_text(
            '{"order_id":"test-order","status":"FAILED","completed":0,"requested":99}'
        )
        job = dict(
            order_id="test-order",
            state="COLLECTED",
            priority=0,
            reason="已取货",
            completed=1,
            total=1,
            finished_sequence=1,
            lines=[dict(sku="corn", quantity=1)],
        )
        self.panel._set_resources(
            json.dumps(dict(bins=self.bins, queue_enabled=True, orders=[job]))
        )
        self.assertEqual(self.panel.history.rowCount(), 1)
        self.assertEqual(self.panel.history.item(0, 1).text(), "玉米×1")
        self.assertEqual(self.panel.history.item(0, 4).text(), "已取货")
        self.assertEqual(self.panel.task_id.text(), "test-order")
        self.assertEqual(self.panel.task_count.text(), "1 / 1")
        self.assertFalse(hasattr(self.panel.worker, "submission"))

    def test_queue_is_on_main_page_sorted_and_registration_receipt_survives_notices(
        self,
    ):
        self.ready()
        jobs = [
            dict(
                order_id=identifier,
                priority=priority,
                state=state,
                completed=0,
                total=1,
                reason="等待调度",
                lines=[dict(sku="corn", quantity=1)],
            )
            for identifier, priority, state in (
                ("ordinary", 1, "QUEUED"),
                ("urgent", 8, "QUEUED"),
                ("active", 0, "RUNNING"),
            )
        ]
        self.panel._set_resources(
            json.dumps(
                dict(bins=self.bins, queue_enabled=True, can_register=True, orders=jobs)
            )
        )
        self.assertEqual(
            [self.panel.queue_table.item(r, 0).text() for r in range(3)],
            ["active", "urgent", "ordinary"],
        )
        self.assertEqual(self.panel.queue_table.item(1, 5).text(), "1")
        self.assertEqual(self.panel.queue_table.item(1, 6).text(), "玉米×1")
        self.assertIn("等待 2", self.panel.queue_summary.text())
        self.assertEqual(self.panel.tabs.count(), 5)
        self.panel._submit()
        identifier = self.panel.worker.submission[0]
        self.panel._on_registered(True, identifier, "已登记")
        self.panel._on_notice("一条后来的普通日志")
        self.assertIn(identifier, self.panel.registration_receipt.text())
        self.assertIn("已登记", self.panel.registration_receipt.text())
        self.assertEqual(self.panel.task_id.text(), "active")

    def test_queued_cancel_response_releases_button_and_removes_confirmed_job(self):
        self.ready()
        job = dict(
            order_id="waiting",
            priority=0,
            state="QUEUED",
            completed=0,
            total=1,
            reason="",
        )
        self.panel._set_resources(
            json.dumps(
                dict(
                    bins=self.bins, queue_enabled=True, can_register=True, orders=[job]
                )
            )
        )
        self.panel.queue_table.selectRow(0)
        self.panel._cancel_queued()
        self.assertFalse(self.panel.queue_cancel_button.isEnabled())
        self.panel._on_operation_finished("cancel_queued", False, "超时，结果未确认")
        self.assertTrue(self.panel.queue_cancel_button.isEnabled())
        self.assertEqual(self.panel.queue_table.rowCount(), 1)
        self.panel._cancel_queued()
        self.panel._on_operation_finished("cancel_queued", True, "已取消")
        self.assertEqual(self.panel.queue_table.rowCount(), 0)
        self.assertIsNone(self.panel._queue_cancel_pending)

    def test_history_clear_persists_preserves_reports_and_protects_live_resources(self):
        from PyQt5.QtWidgets import QMessageBox

        jobs = [
            dict(
                order_id=identifier,
                state=state,
                priority=0,
                reason="",
                completed=0,
                total=1,
            )
            for identifier, state in (
                ("old", "FAILED"),
                ("waiting-pickup", "WAITING_PICKUP"),
                ("needs-recovery", "RECOVERY_REQUIRED"),
            )
        ]
        path = Path(self.temp.name) / "old.json"
        path.write_text('{"export":"preserve"}')
        payload = json.dumps(
            dict(
                bins=[
                    dict(
                        bin_id="A1",
                        order_id="needs-recovery",
                        state="RECOVERY_REQUIRED",
                    )
                ],
                orders=jobs,
            )
        )
        self.panel._set_resources(payload)
        self.assertEqual(self.panel.history.rowCount(), 3)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.panel._clear_history()
        self.assertEqual(self.panel.history.rowCount(), 2)
        self.assertTrue(path.exists())
        self.assertEqual(self.panel._hidden_history, {"old"})
        other = OperatorPanel(
            FakeWorker,
            start_worker=False,
            config=self.config,
            report_dir=Path(self.temp.name),
        )
        try:
            other._set_resources(payload)
            self.assertEqual(other._hidden_history, {"old"})
            other._restore_history()
            self.assertEqual(other.history.rowCount(), 3)
        finally:
            other.close()

    def test_registration_timeout_can_be_confirmed_by_later_server_snapshot(self):
        self.ready()
        self.panel._set_resources(
            json.dumps(
                dict(bins=self.bins, queue_enabled=True, can_register=True, orders=[])
            )
        )
        self.panel._submit()
        identifier = self.panel.worker.submission[0]
        self.panel._on_registered(False, identifier, "登记结果未确认")
        self.assertIn("未确认", self.panel.registration_receipt.text())
        self.panel._set_resources(
            json.dumps(
                dict(
                    bins=self.bins,
                    queue_enabled=True,
                    can_register=True,
                    orders=[
                        dict(
                            order_id=identifier,
                            priority=0,
                            state="QUEUED",
                            completed=0,
                            total=4,
                            reason="等待调度",
                        )
                    ],
                )
            )
        )
        self.assertIn("✓ 已登记", self.panel.registration_receipt.text())

    def test_compact_window_keeps_workstation_on_one_page(self):
        self.panel.resize(1080, 900)
        self.panel.show()
        self.app.processEvents()
        self.assertFalse(self.panel.findChildren(QScrollArea))
        for widget in (
            self.panel.submit_button,
            self.panel.queue_card,
            self.panel.boxes_card,
            self.panel.records,
        ):
            with self.subTest(widget=widget.objectName() or type(widget).__name__):
                self.assertTrue(
                    self.panel.rect().contains(
                        widget.mapTo(self.panel, widget.rect().bottomRight())
                    )
                )
        self.ready()
        self.panel._set_workcell_status(
            "INVENTORY_ERROR", "视觉库存复查失败：Timeout", False, False
        )
        self.app.processEvents()
        self.assertTrue(
            self.panel.rect().contains(
                self.panel.incident_scan.mapTo(
                    self.panel, self.panel.incident_scan.rect().bottomRight()
                )
            )
        )
        self.assertTrue(
            self.panel.rect().contains(
                self.panel.pickup_button.mapTo(
                    self.panel, self.panel.pickup_button.rect().bottomRight()
                )
            )
        )
        self.panel.tabs.setCurrentIndex(4)
        self.app.processEvents()
        for button in self.panel.tabs.widget(4).findChildren(QPushButton):
            with self.subTest(button=button.text()):
                self.assertTrue(
                    self.panel.records.rect().contains(
                        button.mapTo(self.panel.records, button.rect().bottomRight())
                    )
                )

    def test_inventory_is_labeled_as_observations_not_real_stock(self):
        self.panel._set_inventory(
            json.dumps(
                {
                    "epoch": 2,
                    "items": [
                        {
                            "item_id": "obs-2-1",
                            "sku": "corn",
                            "state": "RESERVED",
                            "position": [0.3, 0, 0.04],
                            "frame_id": "base",
                            "age_sec": 3,
                            "order_id": "one",
                        }
                    ],
                }
            )
        )
        self.assertEqual(self.panel.inventory_table.rowCount(), 1)
        self.assertEqual(self.panel.inventory_table.item(0, 2).text(), "已预留")
        self.assertIn("非实物库存保证", self.panel.inventory_hint.text())
        self.panel._last_inventory -= 6
        self.panel._check_status()
        self.assertIn("台账未同步", self.panel.inventory_hint.text())

    def test_queue_mode_allows_new_draft_while_robot_is_busy(self):
        self.ready()
        self.panel._set_workcell_status("RUNNING", "", False, False)
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [
                        dict(
                            order_id="running",
                            priority=1,
                            state="RUNNING",
                            reason="",
                            missing={},
                            completed=1,
                            total=4,
                        )
                    ],
                }
            )
        )
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.assertTrue(self.panel.quantity_spins["corn"].isEnabled())
        self.panel.priority.setValue(10)
        self.panel._submit()
        self.assertEqual(self.panel.worker.submission[-1], 10)
        self.assertEqual(self.panel.task_id.text(), "running")
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.panel._on_registered(True, self.panel.worker.submission[0], "已登记")
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.assertEqual(self.panel.task_count.text(), "1 / 4")

    def test_uncertain_execution_remains_active_and_blocks_recovery(self):
        self.ready()
        self.panel._set_workcell_status(
            "RECOVERY_REQUIRED", "Action 超时，等待核对", False, False
        )
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "can_pickup": False,
                    "orders": [
                        {
                            "order_id": "uncertain",
                            "priority": 0,
                            "state": "EXECUTION_UNCERTAIN",
                            "completed": 1,
                            "total": 3,
                            "reason": "等待实际结果",
                        }
                    ],
                }
            )
        )
        self.assertTrue(self.panel._order_active)
        self.assertEqual(self.panel.task_id.text(), "uncertain")
        self.assertIn("待核对", self.panel.task_status.text())
        self.assertFalse(self.panel.recover_button.isEnabled())

    def test_queue_submit_button_immediately_displays_submission_and_reply(self):
        self.ready()
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [],
                }
            )
        )
        self.panel.submit_button.click()
        identifier = self.panel.worker.submission[0]
        self.assertEqual(self.panel.submit_button.text(), "提交中…")
        self.assertEqual(self.panel.task_status.text(), "提交中")
        self.assertEqual(self.panel.task_id.text(), identifier)
        self.assertIn("正在提交", self.panel.operation_notice.text())
        self.panel._on_registered(True, identifier, "已登记")
        self.assertIn("已登记", self.panel.operation_notice.text())
        self.assertEqual(self.panel.task_status.text(), "已登记 · 等待调度")
        self.assertTrue(self.panel.submit_button.isEnabled())

    def test_registration_timeout_unlocks_and_late_reply_does_not_change_draft(self):
        self.ready()
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [],
                }
            )
        )
        self.panel.submit_button.click()
        identifier = self.panel.worker.submission[0]
        self.panel._on_registered(False, identifier, "登记结果未确认，不要重复下单")
        self.assertFalse(self.panel._registration_pending)
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.assertEqual(self.panel.task_status.text(), "未确认")
        self.panel._on_registered(True, identifier, "late")
        self.assertEqual(self.panel.order_id.text(), identifier)

    def test_status_reconciles_pending_registration_and_shows_waiting_task(self):
        self.ready()
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [],
                }
            )
        )
        self.panel.submit_button.click()
        identifier = self.panel.worker.submission[0]
        job = dict(
            order_id=identifier,
            state="WAITING_STOCK",
            reason="视觉库存复查失败",
            priority=0,
            completed=0,
            total=4,
        )
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [job],
                }
            )
        )
        self.assertFalse(self.panel._registration_pending)
        self.assertEqual(self.panel.task_id.text(), identifier)
        self.assertEqual(self.panel.task_status.text(), "等待库存核对")
        self.assertIn("复查失败", self.panel.failure_reason.text())
        self.assertFalse(self.panel.failure_reason.isHidden())

    def test_old_server_scan_failure_is_not_hidden_as_ready(self):
        self.ready()
        self.panel._set_workcell_status(
            "READY", "视觉库存复查失败：Timeout", True, False
        )
        self.assertEqual(self.panel._state, "INVENTORY_ERROR")
        self.assertIn("复查失败", self.panel.workcell_notice.text())
        self.assertFalse(self.panel.workcell_notice.isHidden())

    def test_inventory_error_has_local_action_and_no_blind_motion_retry(self):
        self.ready()
        self.panel._set_workcell_status(
            "INVENTORY_ERROR", "视觉库存复查失败：Timeout", False, False
        )
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [
                        dict(
                            order_id="waiting",
                            state="WAITING_STOCK",
                            reason="复查失败",
                            priority=0,
                            completed=0,
                            total=4,
                        )
                    ],
                }
            )
        )
        self.assertFalse(self.panel.incident_card.isHidden())
        self.assertIn("库存感知", self.panel.incident_title.text())
        self.assertIn("订单已保留", self.panel.incident_impact.text())
        self.assertFalse(self.panel.incident_scan.isHidden())
        self.assertTrue(self.panel.incident_scan.isEnabled())
        self.panel.incident_scan.click()
        self.assertTrue(self.panel.worker.scan_requested)
        self.assertFalse(self.panel.incident_scan.isEnabled())
        self.panel.worker.operation_finished.emit(
            "refresh_inventory", False, "服务响应未确认"
        )
        self.assertTrue(self.panel.incident_scan.isEnabled())

    def test_incident_cancel_targets_only_current_waiting_order_and_confirms(self):
        from PyQt5.QtWidgets import QMessageBox

        self.ready()
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [
                        dict(
                            order_id="waiting",
                            state="WAITING_STOCK",
                            reason="不足",
                            priority=0,
                            completed=0,
                            total=4,
                        )
                    ],
                }
            )
        )
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.panel.incident_cancel.click()
        self.assertFalse(hasattr(self.panel.worker, "canceled_order"))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.panel.incident_cancel.click()
        self.assertEqual(self.panel.worker.canceled_order, "waiting")

    def test_uncertain_motion_requires_manual_check_and_disables_recovery(self):
        self.ready()
        self.panel._set_workcell_status(
            "RECOVERY_REQUIRED", "夹爪状态未明", False, False
        )
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [
                        dict(
                            order_id="uncertain",
                            state="EXECUTION_UNCERTAIN",
                            reason="状态未明",
                            priority=0,
                            completed=0,
                            total=4,
                        )
                    ],
                }
            )
        )
        self.assertIn("人工核对", self.panel.incident_title.text())
        self.assertIn("不能盲目", self.panel.incident_impact.text())
        self.assertTrue(self.panel.incident_scan.isHidden())
        self.assertTrue(self.panel.incident_cancel.isHidden())
        self.assertFalse(self.panel.incident_recover.isEnabled())

    def test_failed_order_recovery_guides_pickup_then_allows_explicit_reset(self):
        from PyQt5.QtWidgets import QMessageBox

        self.ready()
        self.bins[0].update(state="WAITING_PICKUP", order_id="complete")
        self.bins[1].update(state="RECOVERY_REQUIRED", order_id="failed")
        job = dict(
            order_id="failed",
            state="FAILED",
            reason="test",
            priority=0,
            completed=2,
            total=4,
            finished_sequence=1,
        )
        self.panel._set_workcell_status(
            "RECOVERY_REQUIRED", "已运动，需核对", False, False
        )
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "can_pickup": True,
                    "orders": [job],
                }
            )
        )
        self.assertTrue(self.panel.incident_recover.isEnabled())
        self.assertEqual(self.panel.incident_recover.text(), "先确认取货")
        self.panel.incident_recover.click()
        self.assertIn("A1", self.panel.operation_notice.text())
        self.assertFalse(hasattr(self.panel.worker, "recovery_requested"))
        self.assertTrue(self.panel.pickup_button.isEnabled())
        self.bins[0].update(state="FREE", order_id=None)
        self.panel._set_workcell_status("RECOVERY_REQUIRED", "需核对", False, True)
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [job],
                }
            )
        )
        self.assertEqual(self.panel.incident_recover.text(), "恢复工作台…")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.panel.incident_recover.click()
        self.assertTrue(self.panel.worker.recovery_requested)

    def test_no_duplicate_sidebar_or_yellow_incident_frame(self):
        from PyQt5.QtWidgets import QFrame

        self.assertFalse(self.panel.nav_buttons)
        self.assertIsNone(self.panel.findChild(QFrame, "sidebar"))
        self.assertIsNone(self.panel.findChild(QFrame, "incidentCard"))
        self.assertEqual(self.panel.incident_card.objectName(), "taskSupport")
        self.assertEqual(self.panel.help_menu_button.text(), "诊断与帮助")

    def test_recovery_error_is_not_overwritten_by_generic_confirmation_hint(self):
        self.ready()
        self.panel._set_workcell_status(
            "RECOVERY_REQUIRED", "场景恢复失败：delete_entity 超时", False, True
        )
        self.assertIn("delete_entity", self.panel.workcell_notice.text())

    def test_scanning_is_visible_and_ready_clears_incident(self):
        self.ready()
        self.panel._set_workcell_status("SCANNING", "扫描中", False, False)
        self.assertIn("正在复查", self.panel.incident_title.text())
        self.assertFalse(self.panel.scan_button.isEnabled())
        self.panel._set_workcell_status("READY", "", True, True)
        self.assertTrue(self.panel.incident_card.isHidden())

    def test_camera_check_guides_without_retrying_or_creating_order(self):
        self.ready()
        self.panel._set_workcell_status("INVENTORY_ERROR", "Timeout", False, False)
        self.panel.incident_camera.click()
        self.assertEqual(self.panel.worker.camera_checked, self.config["camera"])
        self.assertFalse(self.panel.incident_camera.isEnabled())
        rows = [
            dict(kind=kind, messages=1 if kind != "depth" else 0)
            for kind in ("color", "depth", "info")
        ]
        self.panel.worker.camera_diagnostic.emit(
            json.dumps({"topics": rows, "all_received": False})
        )
        self.assertIn("深度：未收到", self.panel.camera_result.text())
        self.assertIn("仿真", self.panel.camera_result.text())
        self.assertTrue(self.panel.incident_camera.isEnabled())
        self.assertFalse(hasattr(self.panel.worker, "scan_requested"))
        self.assertFalse(hasattr(self.panel.worker, "submission"))
        for row in rows:
            row["messages"] = 2
        self.panel.worker.camera_diagnostic.emit(
            json.dumps({"topics": rows, "all_received": True})
        )
        self.assertIn("可重试", self.panel.camera_result.text())
        self.assertIn("不代表识别", self.panel.camera_result.text())

    def test_resolution_guide_has_ordered_checks_and_no_blind_reset(self):
        from produce_order_workstation.operator_incident import resolution_steps

        steps = resolution_steps("INVENTORY_ERROR")
        self.assertIn("检查相机", steps[0])
        self.assertIn("原订单会保留", steps[2])
        self.assertIn("不直接恢复", steps[4])
        self.assertIn(
            "确认取货", " ".join(resolution_steps("READY", {"state": "WAITING_BIN"}))
        )

    def test_failed_order_and_waiting_bin_have_distinct_next_steps(self):
        self.ready()
        for state, title, next_step in (
            ("FAILED", "本单失败", "查看日志"),
            ("WAITING_BIN", "箱位", "确认"),
        ):
            self.panel._set_resources(
                json.dumps(
                    {
                        "bins": self.bins,
                        "queue_enabled": True,
                        "can_register": True,
                        "orders": [
                            dict(
                                order_id=state,
                                state=state,
                                reason="test",
                                priority=0,
                                completed=0,
                                total=4,
                                finished_sequence=1,
                            )
                        ],
                    }
                )
            )
            self.assertIn(title, self.panel.incident_title.text())
            self.assertIn(next_step, self.panel.incident_next.text())

    def test_existing_id_in_snapshot_cannot_confirm_a_new_registration(self):
        self.ready()
        job = dict(
            order_id="old-id",
            state="CANCELED",
            reason="old",
            priority=0,
            completed=0,
            total=1,
        )
        status = json.dumps(
            {
                "bins": self.bins,
                "queue_enabled": True,
                "can_register": True,
                "orders": [job],
            }
        )
        self.panel._set_resources(status)
        self.panel.order_id.setText("old-id")
        self.panel.submit_button.click()
        self.panel._set_resources(status)
        self.assertTrue(self.panel._registration_pending)
        self.panel._on_registered(False, "old-id", "订单号已登记")
        self.assertEqual(self.panel.order_id.text(), "old-id")
        self.assertIn("订单号已登记", self.panel.operation_notice.text())

    def test_queue_displays_wait_reason_and_cancel_targets_selected_id(self):
        self.ready()
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [
                        dict(
                            order_id="waiting",
                            priority=2,
                            state="WAITING_STOCK",
                            reason="视觉观测不足",
                            missing={"tomato": 2},
                            completed=0,
                            total=2,
                        )
                    ],
                }
            )
        )
        self.assertIn("番茄", self.panel.queue_table.item(0, 4).text())
        self.panel.queue_table.selectRow(0)
        self.panel._cancel_queued()
        self.assertEqual(self.panel.worker.canceled_order, "waiting")

    def test_clear_queue_targets_only_unstarted_orders_and_requires_confirmation(self):
        from PyQt5.QtWidgets import QMessageBox

        self.ready()
        jobs = [
            dict(
                order_id=state,
                priority=0,
                state=state,
                completed=count,
                total=4,
                reason="",
            )
            for state, count in (
                ("QUEUED", 0),
                ("WAITING_STOCK", 0),
                ("WAITING_BIN", 0),
                ("RUNNING", 1),
                ("PAUSED", 1),
                ("WAITING_PICKUP", 4),
                ("DISPATCHING", 0),
            )
        ]
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": jobs,
                }
            )
        )
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.panel._clear_queue()
        self.assertFalse(hasattr(self.panel.worker, "cleared_ids"))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.panel._clear_queue()
        self.assertEqual(
            self.panel.worker.cleared_ids, ["QUEUED", "WAITING_STOCK", "WAITING_BIN"]
        )
        self.assertFalse(self.panel.queue_clear_button.isEnabled())
        self.panel.worker.queue_cleared.emit(3, 0)
        self.assertFalse(self.panel._queue_clear_pending)

    def test_canceled_jobs_leave_queue_display_but_do_not_delete_resources(self):
        self.ready()
        jobs = [
            dict(
                order_id=state, priority=0, state=state, completed=0, total=1, reason=""
            )
            for state in ("CANCELED", "QUEUED")
        ]
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": jobs,
                }
            )
        )
        self.assertEqual(self.panel.queue_table.rowCount(), 1)
        self.assertEqual(self.panel.queue_table.item(0, 0).text(), "QUEUED")
        self.assertIn("CANCELED", self.panel._queue_jobs)

    def test_clear_logs_clears_only_display_and_button_is_on_log_tabs(self):
        report = Path(self.temp.name) / "preserved.json"
        report.write_text('{"status":"FAILED"}')
        self.panel.events.appendPlainText("event")
        self.panel.technical.appendPlainText("technical")
        self.panel.task_id.setText("keep-task")
        self.panel.tabs.setCurrentIndex(1)
        self.assertFalse(self.panel.clear_logs_button.isHidden())
        self.panel.clear_logs_button.click()
        self.assertEqual(self.panel.events.toPlainText(), "")
        self.assertEqual(self.panel.technical.toPlainText(), "")
        self.assertEqual(self.panel.task_id.text(), "keep-task")
        self.assertTrue(report.exists())
        self.panel.tabs.setCurrentIndex(0)
        self.assertTrue(self.panel.clear_logs_button.isHidden())

    def test_cancel_has_immediate_visible_feedback_and_response_state(self):
        self.ready()
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": [
                        dict(
                            order_id="active",
                            priority=0,
                            state="RUNNING",
                            completed=0,
                            total=4,
                            reason="",
                        )
                    ],
                }
            )
        )
        self.panel.cancel_button.click()
        self.assertTrue(self.panel.worker.cancel_requested)
        self.assertIn("发送取消", self.panel.operation_notice.text())
        self.assertFalse(self.panel.cancel_button.isEnabled())
        self.panel.worker.operation_finished.emit("cancel", True, "已转发取消请求")
        self.assertEqual(self.panel.cancel_button.text(), "等待安全取消")
        self.panel.worker.operation_finished.emit("cancel", False, "响应超时，未确认")
        self.assertEqual(self.panel.cancel_button.text(), "取消")
        self.assertIn("未确认", self.panel.operation_notice.text())

    def test_new_task_removes_old_failure_and_queue_selection_survives_refresh(self):
        self.ready()
        self.panel.task_id.setText("old")
        self.panel.failure_reason.setText("old failure")
        self.panel.failure_reason.show()
        jobs = [
            dict(
                order_id="active",
                priority=0,
                state="RUNNING",
                completed=0,
                total=4,
                reason="",
            ),
            dict(
                order_id="waiting",
                priority=0,
                state="QUEUED",
                completed=0,
                total=4,
                reason="",
            ),
        ]
        status = json.dumps({"bins": self.bins, "queue_enabled": True, "orders": jobs})
        self.panel._set_resources(status)
        self.assertTrue(self.panel.failure_reason.isHidden())
        self.assertEqual(self.panel.failure_reason.text(), "")
        self.panel.queue_table.selectRow(1)
        self.panel._set_resources(status)
        self.assertEqual(
            self.panel.queue_table.item(self.panel.queue_table.currentRow(), 0).text(),
            "waiting",
        )
        self.assertTrue(self.panel.queue_cancel_button.isEnabled())

    def test_latest_result_uses_completion_order_not_submission_order(self):
        self.ready()
        jobs = [
            dict(
                order_id="normal",
                priority=0,
                state="WAITING_PICKUP",
                reason="",
                missing={},
                completed=3,
                total=3,
                finished_sequence=2,
            ),
            dict(
                order_id="urgent",
                priority=10,
                state="WAITING_PICKUP",
                reason="",
                missing={},
                completed=1,
                total=1,
                finished_sequence=1,
            ),
        ]
        self.panel._set_resources(
            json.dumps(
                {
                    "bins": self.bins,
                    "queue_enabled": True,
                    "can_register": True,
                    "orders": jobs,
                }
            )
        )
        self.assertEqual(self.panel.task_id.text(), "normal")

    def test_discard_database_state_does_not_depend_on_receipt_file(self):
        path = Path(self.temp.name) / "partial.json"
        path.write_text(
            '{"order_id":"partial","status":"PAUSED","completed":1,"requested":2}'
        )
        job = dict(
            order_id="partial",
            state="CANCELED",
            priority=0,
            reason="已丢弃",
            completed=1,
            total=2,
        )
        self.panel._set_resources(
            json.dumps(dict(bins=self.bins, queue_enabled=True, orders=[job]))
        )
        self.assertEqual(self.panel.history.rowCount(), 1)
        self.assertEqual(self.panel.history.item(0, 2).text(), "已取消")
        self.assertEqual(self.panel.history.item(0, 4).text(), "已丢弃")
        self.assertEqual(self.panel.task_status.text(), "已取消")

    def test_business_request_selection_survives_status_refresh(self):
        request = dict(
            request_id="req-123",
            kind="request_order_change",
            state="PENDING",
            arguments={},
        )
        payload = json.dumps(
            {"bins": self.bins, "orders": [], "approval_requests": [request]}
        )
        self.panel._set_resources(payload)
        self.panel.business_requests.selectRow(0)
        self.panel._set_resources(payload)
        self.assertEqual(self.panel.business_requests.currentRow(), 0)
        self.assertEqual(self.panel.business_requests.item(0, 1).text(), "修改订单")
        self.panel._on_business_result(
            json.dumps({"success": True, "result": {"answer": "等待复查"}})
        )
        self.assertEqual(self.panel.business_output.toPlainText(), "等待复查")


if __name__ == "__main__":
    unittest.main()
