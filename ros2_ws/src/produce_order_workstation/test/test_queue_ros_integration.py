"""Real ROS transport, fake robot/vision. Run ONLY in an isolated DDS domain.

PRODUCE_ROS_INTEGRATION=1 ROS_DOMAIN_ID=71 pytest .../test_queue_ros_integration.py
No user's order, simulator, model API, or physical entity is touched.
"""

import os
from pathlib import Path
from threading import Thread
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from produce_order_interfaces.srv import SubmitOrder, ConfirmPickup, CancelQueuedOrder
from produce_order_interfaces.msg import OrderLine
from produce_order_workstation.inventory import InventoryLedger
from produce_order_workstation import order_server


pytestmark = pytest.mark.skipif(
    os.getenv("PRODUCE_ROS_INTEGRATION") != "1" or os.getenv("ROS_DOMAIN_ID") != "71",
    reason="Requires explicit isolated ROS domain 71; never target the user's workcell")


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("Isolated ROS integration timed out")


class FakeStation:
    def __init__(self, _connector, config):
        self.config = config
        self.inventory = InventoryLedger()
        self._scene_prepared = True
        self._completed_by_sku = {}
        self._placed_entities = []
        self._used_placement_targets = []
        self.events = []
        self.event_sink = None
        self.cancel_requested = lambda: False
        self.yield_requested = lambda: False
        self.motion_attempted = False
        self.after_first_item = None
        self.execution_slices = []
        self.boxes_created = []
        self.collected = []
        self.points = {"corn": [(.3, -.1, .04), (.3, .1, .04)],
                       "apple": [(.4, -.1, .04), (.4, .1, .04)]}

    def wait_until_ready(self, **_):
        pass

    def prepare_scene(self, *_):
        pass

    def scan_inventory(self, *_):
        placed = [item["position"] for item in self.inventory.snapshot()["items"]
                  if item["state"] in ("PLACED", "COLLECTED")]
        for sku, points in self.points.items():
            self.inventory.observe(sku, self.config["camera"]["target_frame"],
                                   [point for point in points if point not in placed])

    def begin_order(self, _order, *, bin_index):
        self.bin_index = bin_index
        self._completed_by_sku, self._placed_entities = {}, []
        self.motion_attempted = False

    def ensure_order_bins(self, order, indices):
        self.boxes_created.append((order.order_id, indices))

    def synchronize_scene(self):
        pass

    def check_cancel(self):
        pass

    def event(self, order_id, stage, **details):
        entry = dict(order_id=order_id, stage=stage, **details)
        self.events.append(entry)
        if self.event_sink:
            self.event_sink(entry)

    def run(self, order):
        self.execution_slices.append(order.order_id)
        status = "COMPLETED"
        for line in order.lines:
            while self._completed_by_sku.get(line.sku, 0) < line.quantity:
                item = next(item for item in self.inventory.snapshot()["items"]
                            if item["sku"] == line.sku and item["state"] == "RESERVED"
                            and item["order_id"] == order.order_id)
                self.motion_attempted = True
                self.inventory.transition(item["item_id"], order.order_id, "CARRYING")
                self.inventory.transition(item["item_id"], order.order_id, "PLACED")
                self._completed_by_sku[line.sku] = self._completed_by_sku.get(line.sku, 0) + 1
                self._placed_entities.append((line.sku, SimpleNamespace(name=item["item_id"]),
                                              self.bin_index, item["position"]))
                self.event(order.order_id, "ITEM_COMPLETED", completed=len(self._placed_entities),
                           total=order.total_items)
                if self.after_first_item:
                    callback, self.after_first_item = self.after_first_item, None
                    callback()
                if len(self._placed_entities) < order.total_items and self.yield_requested():
                    status = "PAUSED"
                    break
            if status == "PAUSED":
                break
        return dict(status=status, completed=len(self._placed_entities), requested=order.total_items,
                    error="safe yield" if status == "PAUSED" else None, events=self.events)

    def collect_order(self, order, _entities):
        self.collected.append(order.order_id)
        self.inventory.collect(order.order_id)


def test_service_queue_action_feedback_priority_resume_and_pickup(monkeypatch, tmp_path):
    monkeypatch.setattr(order_server, "ROS2Connector", lambda *_, **__: MagicMock())
    monkeypatch.setattr(order_server, "Workstation", FakeStation)
    monkeypatch.setattr(order_server.OrderActionServer, "report_path",
                        staticmethod(lambda identifier: tmp_path / f"{identifier}.json"))
    rclpy.init()
    package = Path(__file__).parents[1]
    import yaml
    config = yaml.safe_load((package / "config/workstation.yaml").read_text())
    config["execution"] = {"backend": "legacy_agent"}
    config["persistence"] = {"enabled": False}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    server = order_server.OrderActionServer(config_path, package / "config/demo_scene.yaml")
    # This regression intentionally substitutes the entire Agent/robot capability.
    server._agent_dispatcher.ready = lambda: True
    client = Node("isolated_queue_test_client")
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(server)
    executor.add_node(client)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    submit = client.create_client(SubmitOrder, "/produce_orders/submit")
    pickup = client.create_client(ConfirmPickup, "/produce_orders/confirm_pickup")
    cancel_queued = client.create_client(CancelQueuedOrder, "/produce_orders/cancel_queued")

    def register(identifier, quantities, priority):
        future = submit.call_async(SubmitOrder.Request(order_id=identifier, zone="A", priority=priority,
            lines=[OrderLine(sku=sku, quantity=count) for sku, count in quantities.items()]))
        wait_for(future.done)
        assert future.result().success

    try:
        assert submit.wait_for_service(timeout_sec=5)
        wait_for(lambda: server._inventory_ready)
        def submit_while_running():
            assert server._busy
            register("isolated-cancel", {"apple": 1}, 1)
            assert server.queue.jobs["isolated-cancel"].state == "QUEUED"
            future = cancel_queued.call_async(CancelQueuedOrder.Request(
                order_id="isolated-cancel", discard_partial=False))
            wait_for(future.done)
            assert future.result().success
            assert server.queue.jobs["isolated-cancel"].state == "CANCELED"
            assert "isolated-cancel" not in server.bin_manager.orders
            register("isolated-urgent", {"apple": 1}, 10)
        server.workstation.after_first_item = submit_while_running
        register("isolated-normal", {"corn": 2, "apple": 1}, 0)
        wait_for(lambda: len(server.queue.jobs) == 3 and all(
            job.state == "WAITING_PICKUP" for job in server.queue.jobs.values()
            if job.order.order_id != "isolated-cancel"))
        assert server.workstation.execution_slices == ["isolated-normal", "isolated-urgent", "isolated-normal"]
        assert len(server.workstation.boxes_created) == 2
        assert server.bin_manager.orders["isolated-normal"].bin_indices == (0,)
        assert server.bin_manager.orders["isolated-urgent"].bin_indices == (1,)
        assert server.queue.jobs["isolated-normal"].completed == 3
        assert server.queue.jobs["isolated-urgent"].completed == 1
        wait_for(lambda: not server._busy and not server._dispatch_order_id)
        future = pickup.call_async(ConfirmPickup.Request(order_id="isolated-normal"))
        wait_for(future.done)
        assert future.result().success
        assert server.bin_manager.bins["A1"].state == "FREE"
        assert server.bin_manager.bins["A2"].state == "WAITING_PICKUP"
        assert server.queue.jobs["isolated-normal"].state == "COLLECTED"
    finally:
        server._dispatch_timer.cancel()
        executor.shutdown(timeout_sec=5)
        thread.join(timeout=5)
        client.destroy_node()
        server.close()
        rclpy.try_shutdown()
