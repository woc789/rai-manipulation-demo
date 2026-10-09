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
    from PyQt5.QtWidgets import QApplication
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

    def cancel(self):
        self.cancel_requested = True

    def submit(self, *args):
        self.submission = args

    def cancel_queued(self, order_id):
        self.canceled_order = order_id

    def clear_queue(self, order_ids):
        self.cleared_ids = order_ids


class OperatorPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.config = yaml.safe_load((Path(__file__).parents[1] / "config/workstation.yaml").read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.panel = OperatorPanel(FakeWorker, start_worker=False, config=self.config, report_dir=Path(self.temp.name))
        self.bins = [{"bin_id": f"A{i+1}", "state": "FREE", "order_id": ""} for i in range(4)]

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
        for button in (self.panel.submit_button, self.panel.pickup_button, self.panel.recover_button):
            self.assertFalse(button.isEnabled())

    def test_capacity_is_inline_and_does_not_change_task(self):
        self.ready()
        self.panel._on_result("COMPLETED", 4, 4, "", "")
        self.panel.quantity_spins["corn"].setValue(4)
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.assertEqual(self.panel.task_count.text(), "4 / 4")
        self.assertEqual(self.panel.progress.value(), 4)
        self.assertEqual(self.panel.task_status.text(), "配货完成")

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

    def test_history_reads_real_reports_and_receipt(self):
        path = Path(self.temp.name) / "test-order.json"
        path.write_text(json.dumps({"order_id": "test-order", "status": "COMPLETED", "completed": 1, "requested": 1,
                                    "lines": [{"sku": "corn", "quantity": 1}]}))
        path.with_name("test-order.pickup.json").write_text('{}')
        self.panel._refresh_history()
        self.assertEqual(self.panel.history.rowCount(), 1)
        self.assertEqual(self.panel.history.item(0, 1).text(), "玉米×1")
        self.assertEqual(self.panel.history.item(0, 4).text(), "已取货")
        self.panel._restore_last_task()
        self.assertEqual(self.panel.task_id.text(), "test-order")
        self.assertEqual(self.panel.task_count.text(), "1 / 1")
        self.assertFalse(hasattr(self.panel.worker, "submission"))

    def test_small_window_has_no_horizontal_page_scroll(self):
        self.panel.resize(1024, 768)
        self.panel.show()
        self.app.processEvents()
        self.assertEqual(self.panel.scroll.horizontalScrollBar().maximum(), 0)

    def test_inventory_is_labeled_as_observations_not_real_stock(self):
        self.panel._set_inventory(json.dumps({"epoch": 2, "items": [{"item_id": "obs-2-1", "sku": "corn",
            "state": "RESERVED", "position": [0.3, 0, 0.04], "frame_id": "base", "age_sec": 3, "order_id": "one"}]}))
        self.assertEqual(self.panel.inventory_table.rowCount(), 1)
        self.assertEqual(self.panel.inventory_table.item(0, 2).text(), "已预留")
        self.assertIn("非实物库存保证", self.panel.inventory_hint.text())
        self.panel._last_inventory -= 6
        self.panel._check_status()
        self.assertIn("台账未同步", self.panel.inventory_hint.text())

    def test_queue_mode_allows_new_draft_while_robot_is_busy(self):
        self.ready()
        self.panel._set_workcell_status("RUNNING", "", False, False)
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
            "can_register": True, "orders": [dict(order_id="running", priority=1, state="RUNNING",
             reason="", missing={}, completed=1, total=4)]}))
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.assertTrue(self.panel.quantity_spins["corn"].isEnabled())
        self.panel.priority.setValue(10)
        self.panel._submit()
        self.assertEqual(self.panel.worker.submission[-1], 10)
        self.assertEqual(self.panel.task_id.text(), "running")
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.panel._on_registered(True, "queued", "已登记")
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.assertEqual(self.panel.task_count.text(), "1 / 4")

    def test_uncertain_execution_remains_active_and_blocks_recovery(self):
        self.ready()
        self.panel._set_workcell_status("RECOVERY_REQUIRED", "Action 超时，等待核对", False, False)
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
            "can_register": True, "can_pickup": False, "orders": [
                {"order_id": "uncertain", "priority": 0, "state": "EXECUTION_UNCERTAIN",
                 "completed": 1, "total": 3, "reason": "等待实际结果"}]}))
        self.assertTrue(self.panel._order_active)
        self.assertEqual(self.panel.task_id.text(), "uncertain")
        self.assertIn("待核对", self.panel.task_status.text())
        self.assertFalse(self.panel.recover_button.isEnabled())

    def test_queue_displays_wait_reason_and_cancel_targets_selected_id(self):
        self.ready()
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
            "can_register": True, "orders": [dict(order_id="waiting", priority=2,
            state="WAITING_STOCK", reason="视觉观测不足", missing={"tomato": 2}, completed=0, total=2)]}))
        self.assertIn("番茄", self.panel.queue_table.item(0, 4).text())
        self.panel.queue_table.selectRow(0)
        self.panel._cancel_queued()
        self.assertEqual(self.panel.worker.canceled_order, "waiting")

    def test_clear_queue_targets_only_unstarted_orders_and_requires_confirmation(self):
        from PyQt5.QtWidgets import QMessageBox
        self.ready()
        jobs = [dict(order_id=state, priority=0, state=state, completed=count, total=4, reason="")
                for state, count in (("QUEUED", 0), ("WAITING_STOCK", 0), ("WAITING_BIN", 0),
                    ("RUNNING", 1), ("PAUSED", 1), ("WAITING_PICKUP", 4), ("DISPATCHING", 0))]
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
            "can_register": True, "orders": jobs}))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.panel._clear_queue()
        self.assertFalse(hasattr(self.panel.worker, "cleared_ids"))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.panel._clear_queue()
        self.assertEqual(self.panel.worker.cleared_ids, ["QUEUED", "WAITING_STOCK", "WAITING_BIN"])
        self.assertFalse(self.panel.queue_clear_button.isEnabled())
        self.panel.worker.queue_cleared.emit(3, 0)
        self.assertFalse(self.panel._queue_clear_pending)

    def test_canceled_jobs_leave_queue_display_but_do_not_delete_resources(self):
        self.ready()
        jobs = [dict(order_id=state, priority=0, state=state, completed=0, total=1, reason="")
                for state in ("CANCELED", "QUEUED")]
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
            "can_register": True, "orders": jobs}))
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
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
            "can_register": True, "orders": [dict(order_id="active", priority=0, state="RUNNING",
                completed=0, total=4, reason="")]}))
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
        jobs = [dict(order_id="active", priority=0, state="RUNNING", completed=0, total=4, reason=""),
                dict(order_id="waiting", priority=0, state="QUEUED", completed=0, total=4, reason="")]
        status = json.dumps({"bins": self.bins, "queue_enabled": True, "orders": jobs})
        self.panel._set_resources(status)
        self.assertTrue(self.panel.failure_reason.isHidden())
        self.assertEqual(self.panel.failure_reason.text(), "")
        self.panel.queue_table.selectRow(1)
        self.panel._set_resources(status)
        self.assertEqual(self.panel.queue_table.item(self.panel.queue_table.currentRow(), 0).text(), "waiting")
        self.assertTrue(self.panel.queue_cancel_button.isEnabled())

    def test_latest_result_uses_completion_order_not_submission_order(self):
        self.ready()
        jobs = [dict(order_id="normal", priority=0, state="WAITING_PICKUP", reason="",
                     missing={}, completed=3, total=3, finished_sequence=2),
                dict(order_id="urgent", priority=10, state="WAITING_PICKUP", reason="",
                     missing={}, completed=1, total=1, finished_sequence=1)]
        self.panel._set_resources(json.dumps({"bins": self.bins, "queue_enabled": True,
                                           "can_register": True, "orders": jobs}))
        self.assertEqual(self.panel.task_id.text(), "normal")

    def test_discard_receipt_is_not_an_extra_order_or_a_still_paused_task(self):
        path = Path(self.temp.name) / "partial.json"
        path.write_text(json.dumps(dict(order_id="partial", status="PAUSED", completed=1, requested=2)))
        path.with_suffix(".discard.json").write_text('{"delivery_state": "DISCARDED"}')
        self.panel._refresh_history()
        self.assertEqual(self.panel.history.rowCount(), 1)
        self.assertEqual(self.panel.history.item(0, 2).text(), "已取消")
        self.assertEqual(self.panel.history.item(0, 4).text(), "已丢弃")
        self.panel._restore_last_task()
        self.assertEqual(self.panel.task_status.text(), "已取消")


if __name__ == "__main__":
    unittest.main()
