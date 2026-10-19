"""Relational business invariants, real SQLite connections and injected failures; no robot/model."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from produce_order_workstation.database import WorkcellDatabase
from produce_order_workstation.inventory_repository import InventoryRepository
from produce_order_workstation.resource_repository import ResourceRepository
from produce_order_workstation.order import Order, OrderLine


CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
)


def order(identifier, count=1):
    return Order(identifier, "A", (OrderLine("corn", count),))


@pytest.fixture
def state(tmp_path):
    store = WorkcellDatabase(tmp_path / "state.db")
    resources = ResourceRepository(store, CONFIG)
    inventory = InventoryRepository(store)
    yield store, resources, inventory
    store.close()


def carrying(state, identifier="one"):
    store, resources, inventory = state
    job = order(identifier)
    resources.submit(job)
    item = inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])[0]
    assert resources.select_next(inventory).order == job
    inventory.begin_execution(item, identifier)
    inventory.transition(item, identifier, "CARRYING")
    operation = store.begin_operation(identifier, "PICK_AND_PLACE")
    return job, item, operation


def test_schema_and_foreign_keys_replace_snapshots(state):
    store, resources, inventory = state
    names = {
        row["name"]
        for row in store.rows("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "orders",
        "order_lines",
        "inventory_items",
        "reservations",
        "bins",
        "bin_allocations",
        "placed_items",
    } <= names
    assert "snapshots" not in names
    assert store.rows("PRAGMA foreign_keys")[0][0] == 1
    item = inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])[0]
    with pytest.raises(sqlite3.IntegrityError):
        inventory.reserve(item, "unregistered")
    assert inventory.snapshot()["items"][0]["state"] == "AVAILABLE"


def test_queries_and_status_age_do_not_write_database(state):
    store, resources, inventory = state
    resources.submit(order("one"))
    inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
    writes = store.db.total_changes
    for _ in range(3):
        resources.snapshot_orders()
        resources.snapshot_bins()
        inventory.snapshot()
        inventory.summary()
    assert store.db.total_changes == writes


def test_order_registration_is_idempotent_and_records_are_detached(state):
    _, resources, _ = state
    first = resources.submit(order("one"))
    assert resources.submit(order("one")).sequence == first.sequence
    with pytest.raises(ValueError):
        resources.submit(order("one", 2))
    with pytest.raises(FrozenInstanceError):
        first.state = "RUNNING"
    resources.update("one", state="WAITING_BIN")
    assert first.state == "QUEUED" and resources.get("one").state == "WAITING_BIN"


def test_missing_stock_leaves_neither_partial_reservation_nor_bin(state):
    store, resources, inventory = state
    resources.submit(order("one", 2))
    inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
    assert resources.select_next(inventory) is None
    assert resources.get("one").state == "WAITING_STOCK"
    assert (
        not store.rows("SELECT * FROM reservations")
        and resources.allocation("one") is None
    )


def test_bin_write_failure_rolls_back_stock_and_order(state):
    store, resources, inventory = state
    resources.submit(order("one"))
    inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_bin BEFORE INSERT ON bin_allocations BEGIN SELECT RAISE(ABORT,'injected');END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        resources.select_next(inventory)
    assert resources.get("one").state == "QUEUED"
    assert not store.rows("SELECT * FROM reservations")
    assert resources.snapshot_bins()[0]["state"] == "FREE"


def test_independent_connections_cannot_double_reserve_stock_or_bins(state):
    store, resources, inventory = state
    resources.submit(order("one"))
    resources.submit(order("two"))
    inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
    other = WorkcellDatabase(store.path)
    other_resources = ResourceRepository(other, CONFIG)
    other_inventory = InventoryRepository(other, run_id=inventory.run_id)
    try:
        with ThreadPoolExecutor(2) as pool:
            jobs = list(
                pool.map(
                    lambda pair: pair[0].select_next(pair[1]),
                    [(resources, inventory), (other_resources, other_inventory)],
                )
            )
        assert sum(job is not None for job in jobs) == 1
        assert (
            len(store.rows("SELECT * FROM reservation_status WHERE state='HELD'")) == 1
        )
        assert (
            len(store.rows("SELECT * FROM bin_allocations WHERE released IS NULL")) == 1
        )
    finally:
        other.close()


def test_nested_call_cannot_commit_outer_rollback(state):
    store, resources, inventory = state
    with pytest.raises(RuntimeError):
        with store.transaction():
            resources.submit(order("one"))
            store.create_request({"kind": "test"})
            raise RuntimeError("abort owner")
    assert resources.get("one") is None and not store.requests()


def test_placement_commits_quantity_inventory_and_operation_once(state):
    store, resources, inventory = state
    job, item, operation = carrying(state)
    inventory.record_placement(
        job,
        item,
        "corn_real",
        "A1",
        (0.4, 0.1, 0.04),
        (0.5, -0.48, 0.1),
        operation,
        "v1",
    )
    assert resources.progress("one") == {"corn": 1}
    assert inventory.snapshot()["items"][0]["state"] == "PLACED"
    assert store.operation(operation)["state"] == "VERIFIED"
    assert resources.allocation("one").entities[0][1].name == "corn_real"
    with pytest.raises(ValueError):
        inventory.record_placement(
            job,
            item,
            "corn_real",
            "A1",
            (0.4, 0.1, 0.04),
            (0.5, -0.48, 0.1),
            operation,
            "v1",
        )
    assert resources.get("one").completed == 1


def test_placement_cannot_consume_another_orders_operation(state):
    store, resources, inventory = state
    job, item, _ = carrying(state)
    resources.submit(order("other"))
    foreign_operation = store.begin_operation("other", "PICK_AND_PLACE")
    with pytest.raises(ValueError, match="this order"):
        inventory.record_placement(
            job,
            item,
            "corn_real",
            "A1",
            (0.4, 0.1, 0.04),
            (0.5, -0.48, 0.1),
            foreign_operation,
            "v1",
        )
    assert resources.progress("one") == {"corn": 0}
    assert not store.rows("SELECT * FROM placed_items")
    assert store.operation(foreign_operation)["state"] == "INTENT"


def test_failed_placement_commit_preserves_intent_not_false_completion(state):
    store, resources, inventory = state
    job, item, operation = carrying(state)
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_progress BEFORE INSERT ON placed_items BEGIN SELECT RAISE(ABORT,'disk failure');END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        inventory.record_placement(
            job,
            item,
            "corn_real",
            "A1",
            (0.4, 0.1, 0.04),
            (0.5, -0.48, 0.1),
            operation,
            "v1",
        )
    assert resources.get("one").completed == 0 and not resources.placements("one")
    assert store.operation(operation)["state"] == "INTENT"
    assert inventory.snapshot()["items"][0]["state"] == "CARRYING"


def test_cancel_failure_rolls_back_release(state):
    store, resources, inventory = state
    job = order("one")
    resources.submit(job)
    inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
    inventory.reserve_order(job)
    with store.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_cancel BEFORE UPDATE OF state ON orders BEGIN SELECT RAISE(ABORT,'cancel failure');END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        resources.cancel("one", inventory)
    assert resources.get("one").state == "QUEUED"
    assert inventory.snapshot()["items"][0]["order_id"] == "one"


def test_restart_keeps_progress_and_ownership_but_requires_scene_review(state):
    store, resources, inventory = state
    job, item, operation = carrying(state)
    inventory.record_placement(
        job,
        item,
        "corn_real",
        "A1",
        (0.4, 0.1, 0.04),
        (0.5, -0.48, 0.1),
        operation,
        "v1",
    )
    resources.finish_allocation("one", "COMPLETED", False)
    resources.finish("one", "COMPLETED", 1, "等待取货")
    other = WorkcellDatabase(store.path)
    try:
        restored = ResourceRepository(other, CONFIG)
        assert restored.recover()
        assert restored.get("one").completed == 1
        assert restored.get("one").state == "RECOVERY_REQUIRED"
        assert restored.snapshot_bins()[0]["order_id"] == "one"
        assert restored.select_next(InventoryRepository(other)) is None
    finally:
        other.close()


@pytest.mark.parametrize("placed", [False, True])
def test_legacy_migration_keeps_orders_and_makes_consistent_backup(tmp_path, placed):
    path = tmp_path / "legacy.db"
    snapshot = dict(
        orders=[
            dict(
                order_id="old",
                zone="A",
                priority=1,
                state="QUEUED",
                reason="等待",
                completed=0,
                lines=[dict(sku="corn", quantity=1)],
            )
        ],
        bins=[],
        allocations=[],
        inventory=dict(
            epoch=3,
            items=[
                dict(
                    item_id="obs-old",
                    sku="corn",
                    frame_id="base",
                    position=[0.4, 0.1, 0.04],
                    observed_unix_sec=1000,
                    state="AVAILABLE",
                    order_id="",
                )
            ],
        ),
    )
    if placed:
        snapshot["orders"][0].update(state="WAITING_PICKUP", completed=1)
        snapshot["inventory"]["items"][0].update(state="PLACED", order_id="old")
        snapshot["allocations"] = [
            dict(
                order_id="old",
                zone="A",
                state="WAITING_PICKUP",
                bin_indices=[0],
                entities=[
                    dict(
                        sku="corn",
                        name="real_corn",
                        bin_index=0,
                        source=[0.4, 0.1, 0.04],
                    )
                ],
            )
        ]
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE snapshots(id INTEGER PRIMARY KEY,body TEXT,updated REAL)"
        )
        db.execute("INSERT INTO snapshots VALUES(1,?,0)", (json.dumps(snapshot),))
    migrated = WorkcellDatabase(path)
    try:
        resources = ResourceRepository(migrated, CONFIG)
        inventory = InventoryRepository(migrated)
        assert resources.get("old").order == order("old")
        assert len(inventory.snapshot()["items"]) == 1
        assert inventory.snapshot()["items"][0]["state"] == (
            "PLACED" if placed else "UNKNOWN"
        )
        if placed:
            assert resources.get("old").completed == 1
            assert migrated.rows("SELECT item_id FROM placed_items")[0][0] == "obs-old"
            assert not migrated.rows("PRAGMA foreign_key_check")
        assert inventory.epoch == 3
        assert not migrated.rows("SELECT 1 FROM sqlite_master WHERE name='snapshots'")
        with sqlite3.connect(str(path) + ".pre-relational.bak") as backup:
            assert (
                json.loads(backup.execute("SELECT body FROM snapshots").fetchone()[0])
                == snapshot
            )
    finally:
        migrated.close()
