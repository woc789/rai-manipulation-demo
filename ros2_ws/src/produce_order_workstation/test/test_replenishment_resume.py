"""Replenishment admits remaining demand, not the full initial stock count.

Physical spawn/vision are controlled fixtures; all business changes below
use real repositories and an isolated SQLite database, never the live DB.
"""

from copy import deepcopy
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from db_support import complete, make_inventory, make_resources
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.order_server import OrderActionServer


CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
)


@pytest.fixture
def paused_server(isolated_business_database):
    store = isolated_business_database
    config = deepcopy(CONFIG)
    config["operation_mode"] = "fulfillment"
    resources = make_resources(config, store=store)
    inventory = make_inventory(store=store)
    station = SimpleNamespace(
        config=config,
        store=store,
        inventory=inventory,
        resources=resources,
        physical_state_uncertain=False,
    )
    order = Order(
        "partial",
        "A",
        (
            OrderLine("corn", 1),
            OrderLine("carrot", 2),
            OrderLine("apple", 2),
            OrderLine("tomato", 1),
        ),
    )
    resources.submit(order)
    for sku in ("corn", "carrot", "carrot", "apple", "tomato"):
        complete(station, order, sku)
    resources.finish_allocation(order.order_id, "PAUSED", False)
    resources.finish(
        order.order_id, "PAUSED", 5, "[REVIEW_REQUIRED] No selectable apple candidate"
    )
    station.replenish_source = MagicMock(
        side_effect=lambda _path: inventory.invalidate_source()
    )
    station.scan_inventory = MagicMock()
    server = SimpleNamespace(
        workstation=station,
        config=config,
        store=store,
        resources=resources,
        scene_path="fixture-scene",
        _lock=RLock(),
        _initial_inventory_required=True,
        _initial_inventory_check={"passed": False},
        _inventory_ready=False,
        _scan_requested=False,
        _resume_review_after_scan=False,
        _busy=True,
        _scene_ready=False,
        _state="REPLENISHING",
        get_logger=MagicMock(),
        publish_status=MagicMock(),
    )

    def set_state(state, reason, *, ready=False):
        server._state, server._state_reason, server._scene_ready = state, reason, ready

    server._set_state = set_state
    return server


@pytest.mark.parametrize("observed_apples", [0, 1, 2])
def test_partial_stock_scanned_once_and_remaining_demand_controls_admission(
    paused_server,
    observed_apples,
):
    server = paused_server
    station = server.workstation
    before = [dict(row) for row in server.store.rows("SELECT * FROM placed_items")]
    initial_bin = server.resources.allocation("partial").bin_indices
    epoch = station.inventory.epoch

    def scan(_skus):
        # Other categories are not observed, but already packed for this order.
        station.inventory.observe(
            "apple",
            "panda_link0",
            [(0.35, 0.2 + index * 0.12, 0.04) for index in range(observed_apples)],
        )

    station.scan_inventory.side_effect = scan
    OrderActionServer._replenish_stock(server)
    assert station.inventory.epoch == epoch + 1
    assert not server._initial_inventory_required
    assert server._initial_inventory_check is None
    assert server._resume_review_after_scan and server._scan_requested
    assert server.resources.get("partial").review_required
    # Spawn success alone has not admitted stock or counted another placement.
    assert server.resources.get("partial").completed == 5
    OrderActionServer._scan_stock(server)
    station.scan_inventory.assert_called_once()
    assert server._state == "READY" and server._inventory_ready
    assert not server.resources.get("partial").review_required
    assert server.resources.get("partial").completed == 5
    assert server.resources.allocation("partial").bin_indices == initial_bin
    assert [
        dict(row) for row in server.store.rows("SELECT * FROM placed_items")
    ] == before
    admitted = server.resources.select_next(station.inventory)
    if observed_apples:
        assert admitted.order.order_id == "partial"
        assert admitted.completed == 5 and admitted.total == 6
        held = server.store.rows(
            "SELECT sku FROM inventory_items WHERE state='RESERVED'"
        )
        assert [row["sku"] for row in held] == ["apple"]
    else:
        assert admitted is None
        assert server.resources.get("partial").state == "WAITING_STOCK"
        assert server.resources.get("partial").missing == {"apple": 1}


def test_scan_failure_does_not_acknowledge_paused_order(paused_server):
    server = paused_server
    OrderActionServer._replenish_stock(server)
    server.workstation.scan_inventory.side_effect = TimeoutError("vision unavailable")
    OrderActionServer._scan_stock(server)
    assert not server._inventory_ready and server._state == "INVENTORY_ERROR"
    assert server.resources.get("partial").review_required
    assert server.resources.get("partial").completed == 5
    assert server.resources.pending() == []


def test_uncertain_motion_is_never_cleared_by_successful_scan(paused_server):
    server = paused_server
    OrderActionServer._replenish_stock(server)
    server.workstation.physical_state_uncertain = True
    OrderActionServer._scan_stock(server)
    assert server._state == "RECOVERY_REQUIRED" and not server._scene_ready
    assert not server._inventory_ready
    assert server.resources.get("partial").review_required
    assert server.resources.pending() == []


def test_unknown_order_stays_quarantined_and_completed_bin_stays_bound(paused_server):
    server = paused_server
    resources = server.resources
    resources.submit(Order("unknown", "A", (OrderLine("corn", 1),)))
    resources.update("unknown", state="RECOVERY_REQUIRED", review_required=True)
    finished = Order("finished", "A", (OrderLine("tomato", 1),))
    resources.submit(finished)
    complete(server.workstation, finished, "tomato")
    resources.finish_allocation("finished", "COMPLETED", False)
    resources.finish("finished", "COMPLETED", 1, "waiting pickup")
    bins = resources.snapshot_bins()
    OrderActionServer._replenish_stock(server)
    OrderActionServer._scan_stock(server)
    assert resources.get("unknown").review_required
    assert resources.get("unknown").state == "RECOVERY_REQUIRED"
    assert resources.get("finished").state == "WAITING_PICKUP"
    assert resources.snapshot_bins() == bins
    assert resources.get("finished").completed == 1


def test_failed_replenishment_never_requests_resume(paused_server):
    server = paused_server
    server.workstation.replenish_source.side_effect = RuntimeError("spawn failed")
    OrderActionServer._replenish_stock(server)
    assert server._state == "RECOVERY_REQUIRED" and not server._scene_ready
    assert not server._scan_requested and not server._resume_review_after_scan
    assert server.resources.get("partial").review_required
    assert server.resources.get("partial").completed == 5
