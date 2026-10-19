"""Schema-two simplification: real SQLite, derived facts and protected history."""

import json
import sqlite3
import time

import pytest

from db_support import CONFIG
from produce_order_workstation.database import WorkcellDatabase
from produce_order_workstation.inventory_repository import InventoryRepository
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.resource_repository import ResourceRepository
from produce_order_workstation.schema import SCHEMA_V1


def order(identifier="one"):
    return Order(identifier, "A", (OrderLine("corn", 2),))


def placed(store):
    resources = ResourceRepository(store, CONFIG)
    inventory = InventoryRepository(store)
    job = order()
    resources.submit(job)
    item = inventory.observe("corn", "base", [(0.4, 0.1, 0.04), (0.4, 0.3, 0.04)])[0]
    resources.select_next(inventory)
    inventory.transition(item, "one", "CARRYING")
    operation = store.begin_operation("one", "PICK_AND_PLACE")
    inventory.record_placement(
        job,
        item,
        "real_corn",
        "A1",
        (0.4, 0.1, 0.04),
        (0.5, -0.48, 0.1),
        operation,
        "v1",
    )
    return resources, inventory, item


def old_database(path):
    """A real v1 database with one placement and one historical counted-only unit."""
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE operations(operation_id TEXT PRIMARY KEY,order_id TEXT NOT NULL,
                phase TEXT NOT NULL,state TEXT NOT NULL,result TEXT,updated REAL NOT NULL);
            CREATE TABLE requests(request_id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL);
            CREATE TABLE events(id INTEGER PRIMARY KEY,order_id TEXT,stage TEXT,body TEXT,created REAL);
        """)
        for statement in SCHEMA_V1:
            db.execute(statement)
        db.execute(
            "INSERT INTO orders(order_id,zone,priority,state,created,updated) VALUES('one','A',0,'RUNNING',1,1)"
        )
        db.execute("INSERT INTO order_lines VALUES('one','corn',3,2)")
        db.execute("INSERT INTO bins VALUES('A1','A',0)")
        db.execute("INSERT INTO bin_allocations VALUES('one','A1','ALLOCATED',1,NULL)")
        db.execute("""INSERT INTO inventory_items(item_id,sku,frame_id,x,y,z,observed,seen_tick,run_id,epoch,state)
                      VALUES('item','corn','base',0.4,0.1,0.04,1,1,'old',1,'PLACED')""")
        db.execute(
            "INSERT INTO reservations(item_id,order_id,state,created,updated) VALUES('item','one','CONSUMED',1,1)"
        )
        db.execute(
            "INSERT INTO operations VALUES('op','one','PICK_AND_PLACE','VERIFIED',?,1)",
            (json.dumps({"completed_by_sku": {"corn": 2}, "status": "SUCCESS"}),),
        )
        db.execute(
            "INSERT INTO placed_items VALUES('item','one','corn','A1','real_corn','[0.4,0.1,0.04]','[0.5,-0.48,0.1]','op',1)"
        )
        db.execute(
            "INSERT INTO events VALUES(1,'one','ITEM_COMPLETED',?,1)",
            (
                json.dumps(
                    {"order_id": "one", "stage": "ITEM_COMPLETED", "completed": 2}
                ),
            ),
        )
        db.execute("PRAGMA user_version=1")


def test_v1_migration_preserves_baseline_without_duplicate_placement(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)
    store = WorkcellDatabase(path)
    try:
        resources = ResourceRepository(store, CONFIG)
        assert store.rows("PRAGMA user_version")[0][0] == 2
        assert resources.progress("one") == {"corn": 2}
        assert store.rows("SELECT legacy_completed FROM order_lines")[0][0] == 1
        assert len(store.rows("SELECT * FROM placed_items")) == 1
        assert resources.allocation("one").state == "ALLOCATED"
        assert store.rows("SELECT state FROM reservation_status")[0][0] == "CONSUMED"
        assert "completed_by_sku" not in store.operation("op")["result"]
        assert "order_id" not in json.loads(store.rows("SELECT body FROM events")[0][0])
        assert store.events_for_order("one")[0]["order_id"] == "one"
        assert not store.rows("PRAGMA foreign_key_check")
        assert store.rows("PRAGMA integrity_check")[0][0] == "ok"
        with sqlite3.connect(str(path) + ".pre-v2.bak") as backup:
            assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
            assert (
                backup.execute("SELECT completed FROM order_lines").fetchone()[0] == 2
            )
    finally:
        store.close()
    reopened = WorkcellDatabase(path)
    try:
        assert ResourceRepository(reopened, CONFIG).progress("one") == {"corn": 2}
    finally:
        reopened.close()


def test_migration_failure_rolls_back_schema_and_values(tmp_path, monkeypatch):
    from produce_order_workstation import schema

    path = tmp_path / "fault.db"
    old_database(path)
    upgrade = schema.upgrade_v2

    def fault(store):
        upgrade(store)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(schema, "upgrade_v2", fault)
    with pytest.raises(RuntimeError, match="migration failure"):
        WorkcellDatabase(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT completed FROM order_lines").fetchone()[0] == 2
        assert db.execute("SELECT state FROM reservations").fetchone()[0] == "CONSUMED"
    with pytest.raises(ValueError, match="backup already exists"):
        WorkcellDatabase(path)


def test_new_progress_is_placement_count_not_a_second_counter():
    store = WorkcellDatabase(":memory:")
    try:
        resources, inventory, item = placed(store)
        assert resources.progress("one") == {"corn": 1}
        assert store.rows("SELECT legacy_completed FROM order_lines")[0][0] == 0
        assert "completed" not in {
            r["name"] for r in store.rows("PRAGMA table_info(order_lines)")
        }
        assert "version" not in {
            r["name"] for r in store.rows("PRAGMA table_info(inventory_items)")
        }
        assert "state" not in {
            r["name"] for r in store.rows("PRAGMA table_info(reservations)")
        }
        assert store.rows("SELECT state FROM reservation_status")[0][0] == "CONSUMED"
        operation = store.rows("SELECT operation_id FROM placed_items")[0][0]
        assert "completed_by_sku" not in store.operation(operation)["result"]
        inventory.collect("one")
        assert resources.progress("one") == {"corn": 1}
        assert store.rows("SELECT state FROM reservation_status")[0][0] == "CONSUMED"
        assert inventory.snapshot()["items"][0]["item_id"] == item
    finally:
        store.close()


def test_observations_off_by_default_and_bounded_when_enabled():
    store = WorkcellDatabase(":memory:")
    try:
        inventory = InventoryRepository(store)
        inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
        assert not store.rows(
            "SELECT 1 FROM sqlite_master WHERE name='inventory_observations'"
        )
        debug = InventoryRepository(
            store, record_observations=True, observation_limit=2
        )
        for _ in range(5):
            debug.observe("corn", "base", [(0.4, 0.1, 0.04)])
        assert store.rows("SELECT count(*) FROM inventory_observations")[0][0] == 2
        assert len(debug.snapshot()["items"]) == 1
        inventory.observe("corn", "base", [(0.4, 0.1, 0.04)])
        assert store.rows("SELECT count(*) FROM inventory_observations")[0][0] == 2
    finally:
        store.close()


def test_compact_json_keeps_public_event_and_request_shape():
    store = WorkcellDatabase(":memory:")
    try:
        entry = {
            "order_id": "one",
            "stage": "TEST",
            "time_utc": "now",
            "detail": "info",
        }
        store.event(entry)
        raw = json.loads(store.rows("SELECT body FROM events")[0][0])
        assert "order_id" not in raw and "stage" not in raw
        assert store.events_for_order("one") == [entry]
        body = {"kind": "test", "arguments": {}}
        request = store.create_request(body)
        assert store.create_request(body) == request
        assert "request_id" not in json.loads(
            store.rows("SELECT body FROM requests")[0][0]
        )
        assert (
            store.request(request["request_id"])["request_id"] == request["request_id"]
        )
        assert store.requests()[0]["request_id"] == request["request_id"]
    finally:
        store.close()


def test_retention_protects_active_unknown_approval_and_key_evidence():
    store = WorkcellDatabase(":memory:", max_events=2)
    try:
        resources = ResourceRepository(store, CONFIG)
        for name in ("settled", "active", "uncertain", "approval", "review"):
            resources.submit(order(name))
            if name != "active":
                resources.update(name, state="FAILED", review_required=name == "review")
            store.event({"order_id": name, "stage": "DETAIL", "detail": name})
        pending = store.begin_operation("uncertain", "PICK_AND_PLACE")
        store.finish_operation(pending, "UNCERTAIN")
        store.create_request(
            {"kind": "request_order_change", "arguments": {"order_id": "approval"}}
        )
        store.event(
            {"order_id": "settled", "stage": "ORDER_FAILED", "reason": "evidence"}
        )
        store.event({"order_id": "", "stage": "DEBUG", "detail": "disposable"})
        assert store.prune_history(now=time.time() + 8 * 86400) == 2
        rows = {
            (r["order_id"], r["stage"])
            for r in store.rows("SELECT order_id,stage FROM events")
        }
        assert ("settled", "DETAIL") not in rows
        assert ("settled", "ORDER_FAILED") in rows
        assert all(
            (name, "DETAIL") in rows
            for name in ("active", "uncertain", "approval", "review")
        )
        assert store.operation(pending)["state"] == "UNCERTAIN"
        assert len(store.requests()) == 1
        assert store.rows("SELECT count(*) FROM events")[0][0] > store.max_events
    finally:
        store.close()


def test_database_trigger_enforces_demand_without_a_mutable_count():
    store = WorkcellDatabase(":memory:")
    try:
        resources, _, _ = placed(store)
        with store.transaction() as db:
            db.execute("UPDATE order_lines SET quantity=1 WHERE order_id='one'")
            db.execute("""INSERT INTO inventory_items(item_id,sku,frame_id,x,y,z,observed,seen_tick,run_id,epoch,state)
                          VALUES('second','corn','base',0.5,0.1,0.04,1,1,'test',1,'CARRYING')""")
        with pytest.raises(sqlite3.IntegrityError, match="exceeds"):
            with store.transaction() as db:
                db.execute(
                    "INSERT INTO placed_items VALUES('second','one','corn','A1','second','[]','[]',NULL,1)"
                )
        assert resources.progress("one") == {"corn": 1}
    finally:
        store.close()


def test_uncertain_maintenance_blocks_cleanup_even_for_global_events():
    store = WorkcellDatabase(":memory:", max_events=1)
    try:
        request = store.create_request(
            {"kind": "request_replenishment", "arguments": {}}
        )
        store.admit_maintenance(request["request_id"])
        store.execution_result(request["request_id"], "UNKNOWN")
        store.event({"order_id": "", "stage": "DEBUG", "detail": "old scene"})
        store.event({"order_id": "", "stage": "DEBUG", "detail": "new scene"})
        assert store.prune_history(now=time.time() + 8 * 86400) == 0
        assert len(store.rows("SELECT * FROM events")) == 2
    finally:
        store.close()


def test_review_reservation_is_not_upgraded_to_usable_ownership(tmp_path):
    path = tmp_path / "review.db"
    old_database(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE inventory_items SET state='RESERVED'")
        db.execute("UPDATE reservations SET state='REVIEW'")
    store = WorkcellDatabase(path)
    try:
        assert store.rows("SELECT state FROM inventory_items")[0][0] == "UNKNOWN"
        assert store.rows("SELECT state FROM reservation_status")[0][0] == "REVIEW"
    finally:
        store.close()
