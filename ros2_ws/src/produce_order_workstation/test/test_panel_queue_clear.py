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


if __name__ == "__main__":
    unittest.main()
