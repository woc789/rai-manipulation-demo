"""Real ROS transport, fake robot/vision. Run ONLY in an isolated DDS domain.

PRODUCE_ROS_INTEGRATION=1 ROS_DOMAIN_ID=71 pytest .../test_queue_ros_integration.py
No user's order, simulator, model API, or physical entity is touched.
"""

import os
from pathlib import Path
from threading import Thread
import time
from unittest.mock import MagicMock

import pytest
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node

from produce_order_interfaces.srv import SubmitOrder, ConfirmPickup, CancelQueuedOrder
from produce_order_interfaces.msg import OrderLine
from db_support import mark_placed
from produce_order_workstation import order_server
from produce_order_workstation.runtime import Workstation


pytestmark = pytest.mark.skipif(
    os.getenv("PRODUCE_ROS_INTEGRATION") != "1" or os.getenv("ROS_DOMAIN_ID") != "71",
    reason="Requires explicit isolated ROS domain 71; never target the user's workcell",
)


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Isolated ROS integration timed out")


class FakeStation(Workstation):
    """Fake physical skills, production SQLite-backed runtime state."""

    def __init__(self, connector, config, *, store):
        super().__init__(connector, config, store=store)
        self._scene_prepared = True
        self.after_first_item = None
        self.execution_slices = []
        self.boxes_created = []
        self.collected = []
        self.points = {
            "corn": [(0.3, -0.1, 0.04), (0.3, 0.1, 0.04)],
            "apple": [(0.4, -0.1, 0.04), (0.4, 0.1, 0.04)],
        }

    def wait_until_ready(self, **_):
        pass

    def prepare_scene(self, *_):
        pass

    def synchronize_scene(self):
        pass

    def scan_inventory(self, *_):
        placed = [
            item["position"]
            for item in self.inventory.snapshot()["items"]
            if item["state"] in ("PLACED", "COLLECTED")
        ]
        for sku, points in self.points.items():
            self.inventory.observe(
                sku,
                self.config["camera"]["target_frame"],
                [point for point in points if list(point) not in placed],
            )

    def begin_order(self, order, *, bin_index):
        super().begin_order(order, bin_index=bin_index)
        self.bin_index = bin_index

    def ensure_order_bins(self, order, indices):
        if not any(row[0] == order.order_id for row in self.boxes_created):
            self.boxes_created.append((order.order_id, indices))

    def run(self, order):
        self.execution_slices.append(order.order_id)
        status = "COMPLETED"
        for line in order.lines:
            while self._completed_by_sku.get(line.sku, 0) < line.quantity:
                item = next(
                    item
                    for item in self.inventory.snapshot()["items"]
                    if item["sku"] == line.sku
                    and item["state"] == "RESERVED"
                    and item["order_id"] == order.order_id
                )
                self.motion_attempted = True
                self.inventory.transition(item["item_id"], order.order_id, "CARRYING")
                mark_placed(self.inventory, item["item_id"], order.order_id)
                self.event(
                    order.order_id,
                    "ITEM_COMPLETED",
                    completed=len(self._placed_entities),
                    total=order.total_items,
                )
                if self.after_first_item:
                    callback, self.after_first_item = self.after_first_item, None
                    callback()
                if (
                    len(self._placed_entities) < order.total_items
                    and self.yield_requested()
                ):
                    status = "PAUSED"
                    break
            if status == "PAUSED":
                break
        return dict(
            status=status,
            completed=len(self._placed_entities),
            requested=order.total_items,
            error="safe yield" if status == "PAUSED" else None,
            events=self.events,
        )

    def collect_order(self, order, _entities):
        self.collected.append(order.order_id)


def test_service_queue_action_feedback_priority_resume_and_pickup(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(order_server, "ROS2Connector", lambda *_, **__: MagicMock())
    monkeypatch.setattr(order_server, "Workstation", FakeStation)
    monkeypatch.setattr(
        order_server.OrderActionServer,
        "report_path",
        staticmethod(lambda identifier: tmp_path / f"{identifier}.json"),
    )
    rclpy.init()
    package = Path(__file__).parents[1]
    import yaml

    config = yaml.safe_load((package / "config/workstation.yaml").read_text())
    config["execution"] = {"backend": "legacy_agent"}
    config["persistence"] = {"path": str(tmp_path / "queue.db")}
    # This isolated transport test has exactly two synthetic corn/apple each;
    # the production acceptance gate must validate that declared fixture too.
    config["stock"]["initial_counts"] = {"corn": 2, "apple": 2}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    server = order_server.OrderActionServer(
        config_path, package / "config/demo_scene.yaml"
    )
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
    cancel_queued = client.create_client(
        CancelQueuedOrder, "/produce_orders/cancel_queued"
    )

    def register(identifier, quantities, priority):
        future = submit.call_async(
            SubmitOrder.Request(
                order_id=identifier,
                zone="A",
                priority=priority,
                lines=[
                    OrderLine(sku=sku, quantity=count)
                    for sku, count in quantities.items()
                ],
            )
        )
        wait_for(future.done)
        assert future.result().success

    try:
        assert submit.wait_for_service(timeout_sec=5)
        wait_for(lambda: server._inventory_ready)

        def submit_while_running():
            assert server._busy
            register("isolated-cancel", {"apple": 1}, 1)
            assert server.resources.get("isolated-cancel").state == "QUEUED"
            future = cancel_queued.call_async(
                CancelQueuedOrder.Request(
                    order_id="isolated-cancel", discard_partial=False
                )
            )
            wait_for(future.done)
            assert future.result().success
            assert server.resources.get("isolated-cancel").state == "CANCELED"
            assert server.resources.allocation("isolated-cancel") is None
            register("isolated-urgent", {"apple": 1}, 10)

        server.workstation.after_first_item = submit_while_running
        register("isolated-normal", {"corn": 2, "apple": 1}, 0)
        wait_for(
            lambda: len(server.resources.all()) == 3
            and all(
                job.state == "WAITING_PICKUP"
                for job in server.resources.all()
                if job.order.order_id != "isolated-cancel"
            )
        )
        assert server.workstation.execution_slices == [
            "isolated-normal",
            "isolated-urgent",
            "isolated-normal",
        ]
        assert len(server.workstation.boxes_created) == 2
        assert server.resources.allocation("isolated-normal").bin_indices == (0,)
        assert server.resources.allocation("isolated-urgent").bin_indices == (1,)
        assert server.resources.get("isolated-normal").completed == 3
        assert server.resources.get("isolated-urgent").completed == 1
        wait_for(lambda: not server._busy and not server._dispatch_order_id)
        future = pickup.call_async(ConfirmPickup.Request(order_id="isolated-normal"))
        wait_for(future.done)
        assert future.result().success
        assert server.resources.bin("A1").state == "FREE"
        assert server.resources.bin("A2").state == "WAITING_PICKUP"
        assert server.resources.get("isolated-normal").state == "COLLECTED"
    finally:
        server._dispatch_timer.cancel()
        executor.shutdown(timeout_sec=5)
        thread.join(timeout=5)
        client.destroy_node()
        server.close()
        rclpy.try_shutdown()
