"""Mock ROS services only. No live queue cancellation or robot motion."""
from concurrent.futures import Future
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

if find_spec("PyQt5") is None:
    raise unittest.SkipTest("Qt worker tests use /usr/bin/python3 with installed PyQt5")

from produce_order_workstation.debug_panel import RosWorker


class QueueClearTests(unittest.TestCase):
    def setUp(self):
        self.worker = RosWorker()
        self.results = []
        self.worker.queue_cleared.connect(lambda a, b: self.results.append((a, b)))
        self.client = MagicMock()
        self.client.service_is_ready.return_value = True

    def test_success_rejection_and_duplicate_ids(self):
        replies = [Future(), Future()]
        self.client.call_async.side_effect = replies
        self.worker._clear_queue(self.client, ["waiting", "became-running", "waiting"])
        replies[0].set_result(SimpleNamespace(success=True))
        replies[1].set_result(SimpleNamespace(success=False))
        self.assertEqual(self.results, [(1, 1)])
        requests = [call.args[0] for call in self.client.call_async.call_args_list]
        self.assertEqual([r.order_id for r in requests], ["waiting", "became-running"])
        self.assertTrue(all(not r.discard_partial for r in requests))

    def test_timeout_finishes_ui_wait_and_ignores_late_reply(self):
        reply = Future()
        self.client.call_async.return_value = reply
        self.worker._clear_queue(self.client, ["waiting"])
        deadline = self.worker._clear_queue_batch["deadline"]
        with patch("produce_order_workstation.debug_panel.time.monotonic", return_value=deadline + 1):
            self.worker._check_clear_queue_timeout()
        self.assertEqual(self.results, [(0, 1)])
        self.assertFalse(self.worker._clearing_queue)
        reply.set_result(SimpleNamespace(success=True))
        self.assertEqual(self.results, [(0, 1)])

    def test_offline_and_send_exception_do_not_stick(self):
        self.client.service_is_ready.return_value = False
        self.worker._clear_queue(self.client, ["a"])
        self.assertEqual(self.results, [(0, 1)])
        self.client.call_async.assert_not_called()
        self.client.service_is_ready.return_value = True
        self.client.call_async.side_effect = RuntimeError("offline")
        self.worker._clear_queue(self.client, ["a"])
        self.assertFalse(self.worker._clearing_queue)
        self.assertEqual(self.results, [(0, 1), (0, 1)])

    def test_empty_batch(self):
        self.worker._clear_queue(self.client, [])
        self.assertEqual(self.results, [(0, 0)])

    def test_single_control_request_times_out_and_ignores_late_response(self):
        replies = []
        self.worker.operation_finished.connect(lambda *result: replies.append(result))
        reply = Future()
        self.client.call_async.return_value = reply
        self.worker._trigger(self.client, "cancel")
        deadline = self.worker._service_waits[reply][2]
        with patch("produce_order_workstation.debug_panel.time.monotonic", return_value=deadline + 1):
            self.worker._check_service_timeouts()
        self.assertEqual(replies[0][0:2], ("cancel", False))
        self.client.remove_pending_request.assert_called_once_with(reply)
        reply.set_result(SimpleNamespace(success=True, message="late"))
        self.assertEqual(len(replies), 1)

    def test_registration_timeout_reports_unknown_and_ignores_late_reply(self):
        results = []
        self.worker.registered.connect(lambda *result: results.append(result))
        reply = Future()
        self.client.call_async.return_value = reply
        self.worker._register(self.client, ("submit", "reg-test", {"corn": 1}, True, 0))
        deadline = self.worker._registration_waits[reply][2]
        with patch("produce_order_workstation.debug_panel.time.monotonic", return_value=deadline + 1):
            self.worker._check_service_timeouts()
        self.assertEqual(results[0][0:2], (False, "reg-test"))
        self.assertIn("未确认", results[0][2])
        self.client.remove_pending_request.assert_called_once_with(reply)
        reply.set_result(SimpleNamespace(success=True, message="late"))
        self.assertEqual(len(results), 1)

    def test_registration_success_and_send_failure_release_wait(self):
        results = []
        self.worker.registered.connect(lambda *result: results.append(result))
        reply = Future()
        self.client.call_async.return_value = reply
        self.worker._register(self.client, ("submit", "reg-test", {"corn": 1}, True, 0))
        reply.set_result(SimpleNamespace(success=True, message="已登记"))
        self.assertEqual(results[-1], (True, "reg-test", "已登记"))
        self.assertFalse(self.worker._registration_waits)
        self.client.call_async.side_effect = RuntimeError("fake send failure")
        self.worker._register(self.client, ("submit", "reg-test-2", {"corn": 1}, True, 0))
        self.assertFalse(results[-1][0])
        self.assertIn("未发送", results[-1][2])

    def test_camera_probe_counts_messages_cleans_subscriptions_and_does_not_call_service(self):
        node = MagicMock()
        reports = []
        self.worker.camera_diagnostic.connect(reports.append)
        self.worker._start_camera_check(node, {"color": "/c", "depth": "/d", "info": "/i"})
        callbacks = [entry.args[2] for entry in node.create_subscription.call_args_list]
        callbacks[0](None)
        callbacks[1](None)
        deadline = self.worker._camera_probe["deadline"]
        with patch("produce_order_workstation.debug_panel.time.monotonic", return_value=deadline + 1):
            self.worker._poll_camera_check(node)
        import json
        report = json.loads(reports[0])
        self.assertFalse(report["all_received"])
        self.assertEqual([r["messages"] for r in report["topics"]], [1, 1, 0])
        self.assertEqual(node.destroy_subscription.call_count, 3)
        self.assertIsNone(self.worker._camera_probe)
        node.create_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
