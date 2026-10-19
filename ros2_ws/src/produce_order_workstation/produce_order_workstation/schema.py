"""Version-one bootstrap/import and transactional upgrade to lean schema two."""

import json
import re
import time
import uuid


OBSERVATION_SCHEMA = """CREATE TABLE inventory_observations(
    observation_id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES inventory_items(item_id),
    sku TEXT NOT NULL,frame_id TEXT NOT NULL,x REAL NOT NULL,y REAL NOT NULL,z REAL NOT NULL,
    observed REAL NOT NULL,run_id TEXT NOT NULL,epoch INTEGER NOT NULL,source TEXT NOT NULL)"""


SCHEMA_V1 = (
    "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE orders(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
        zone TEXT NOT NULL, priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 10),
        state TEXT NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','WAITING_STOCK','WAITING_BIN','DISPATCHING',
            'RUNNING','PAUSED','WAITING_PICKUP','RECOVERY_REQUIRED','EXECUTION_UNCERTAIN','COLLECTED',
            'FAILED','CANCELED','CANCELLED','ALLOCATED','DISCARDED_BY_RESET','COMPLETED')),
        reason TEXT NOT NULL DEFAULT '等待调度',
        missing TEXT NOT NULL DEFAULT '{}', review_required INTEGER NOT NULL DEFAULT 0 CHECK(review_required IN (0,1)),
        finished_sequence INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
        unattributed_completed INTEGER NOT NULL DEFAULT 0 CHECK(unattributed_completed>=0),
        created REAL NOT NULL, updated REAL NOT NULL)""",
    """CREATE TABLE order_lines(
        order_id TEXT NOT NULL REFERENCES orders(order_id), sku TEXT NOT NULL,
        quantity INTEGER NOT NULL CHECK(quantity>0), completed INTEGER NOT NULL DEFAULT 0,
        CHECK(completed>=0 AND completed<=quantity), PRIMARY KEY(order_id,sku))""",
    """CREATE TABLE inventory_items(
        item_id TEXT PRIMARY KEY, sku TEXT NOT NULL, frame_id TEXT NOT NULL,
        x REAL NOT NULL,y REAL NOT NULL,z REAL NOT NULL, observed REAL NOT NULL,
        seen_tick REAL NOT NULL, run_id TEXT NOT NULL, epoch INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('AVAILABLE','RESERVED','CARRYING','PLACED','COLLECTED','UNKNOWN','RETIRED')),
        verification TEXT NOT NULL DEFAULT 'FRESH', executing INTEGER NOT NULL DEFAULT 0 CHECK(executing IN (0,1)),
        version INTEGER NOT NULL DEFAULT 1)""",
    OBSERVATION_SCHEMA,
    """CREATE TABLE reservations(
        reservation_id INTEGER PRIMARY KEY,item_id TEXT NOT NULL REFERENCES inventory_items(item_id),
        order_id TEXT NOT NULL REFERENCES orders(order_id),
        state TEXT NOT NULL CHECK(state IN ('HELD','CARRYING','CONSUMED','REVIEW','RELEASED')),
        created REAL NOT NULL,updated REAL NOT NULL)""",
    "CREATE UNIQUE INDEX one_item_owner ON reservations(item_id) WHERE state<>'RELEASED'",
    "CREATE INDEX reservation_order ON reservations(order_id,state)",
    "CREATE INDEX inventory_select ON inventory_items(epoch,sku,state)",
    "CREATE INDEX observations_item ON inventory_observations(item_id,observed)",
    """CREATE TABLE bins(
        bin_id TEXT PRIMARY KEY,zone TEXT NOT NULL,bin_index INTEGER NOT NULL CHECK(bin_index>=0),
        UNIQUE(zone,bin_index))""",
    """CREATE TABLE bin_allocations(
        order_id TEXT NOT NULL REFERENCES orders(order_id),bin_id TEXT NOT NULL REFERENCES bins(bin_id),
        state TEXT NOT NULL CHECK(state IN ('ALLOCATED','PAUSED','WAITING_PICKUP','RECOVERY_REQUIRED','COLLECTED',
            'DISCARDED_BY_RESET','FAILED','CANCELED','CANCELLED')),created REAL NOT NULL,released REAL,
        PRIMARY KEY(order_id,bin_id))""",
    "CREATE UNIQUE INDEX one_bin_owner ON bin_allocations(bin_id) WHERE released IS NULL",
    """CREATE TABLE placed_items(
        item_id TEXT PRIMARY KEY REFERENCES inventory_items(item_id),order_id TEXT NOT NULL REFERENCES orders(order_id),
        sku TEXT NOT NULL,bin_id TEXT NOT NULL REFERENCES bins(bin_id),
        entity_name TEXT NOT NULL,source TEXT NOT NULL,target TEXT NOT NULL,
        operation_id TEXT UNIQUE REFERENCES operations(operation_id),verified REAL NOT NULL,
        FOREIGN KEY(order_id,sku) REFERENCES order_lines(order_id,sku))""",
    "CREATE INDEX placed_order ON placed_items(order_id,verified)",
    "CREATE INDEX orders_dispatch ON orders(state,priority DESC,sequence)",
    "CREATE INDEX event_order ON events(order_id,id)",
)


def import_legacy(store, snapshot):
    """Only called inside schema migration. Coordinates never become AVAILABLE."""
    db, now = store.db, time.time()
    checkpoint = snapshot.get("checkpoint", {})
    order_rows = {row["order_id"]: dict(row) for row in snapshot.get("orders", [])}
    for allocation in snapshot.get("allocations", []):
        order_rows.setdefault(
            allocation["order_id"],
            dict(
                allocation,
                priority=0,
                completed=0,
                reason="历史直接执行订单导入，需核对",
                lines=allocation.get("lines", []),
            ),
        )
    for identifier, row in order_rows.items():
        if not row.get("lines"):
            raise ValueError(
                f"Legacy order {identifier} has no lines; migration rolled back"
            )
        progress = legacy_verified_progress(db, identifier)
        if checkpoint.get("order_id") == identifier:
            for sku, count in checkpoint.get("completed_by_sku", {}).items():
                progress[sku] = max(progress.get(sku, 0), count)
        total = sum(line["quantity"] for line in row["lines"])
        if row.get("completed") == total:
            progress = {line["sku"]: line["quantity"] for line in row["lines"]}
        confirmed = sum(
            min(line["quantity"], progress.get(line["sku"], 0)) for line in row["lines"]
        )
        db.execute(
            """INSERT INTO orders(order_id,zone,priority,state,reason,review_required,
                   finished_sequence,unattributed_completed,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                row.get("zone", "A"),
                row.get("priority", 0),
                row.get("state", "RECOVERY_REQUIRED"),
                row.get("reason", "导入历史状态"),
                bool(row.get("review_required")),
                row.get("finished_sequence", 0),
                max(0, row.get("completed", 0) - confirmed),
                now,
                now,
            ),
        )
        for line in row["lines"]:
            db.execute(
                "INSERT INTO order_lines VALUES(?,?,?,?)",
                (
                    identifier,
                    line["sku"],
                    line["quantity"],
                    min(line["quantity"], progress.get(line["sku"], 0)),
                ),
            )

    def bin_id(identifier):
        match = re.fullmatch(r"(.+?)(\d+)", identifier)
        if not match or int(match[2]) < 1:
            raise ValueError("Invalid legacy bin ID")
        db.execute(
            "INSERT OR IGNORE INTO bins VALUES(?,?,?)",
            (identifier, match[1], int(match[2]) - 1),
        )

    for row in snapshot.get("bins", []):
        bin_id(row["bin_id"])
    for allocation in snapshot.get("allocations", []):
        identifier = allocation["order_id"]
        for index in allocation.get("bin_indices", []):
            name = f"{allocation.get('zone', 'A')}{index + 1}"
            bin_id(name)
            state = allocation["state"]
            released = (
                None
                if state
                in ("ALLOCATED", "PAUSED", "WAITING_PICKUP", "RECOVERY_REQUIRED")
                else now
            )
            db.execute(
                "INSERT INTO bin_allocations VALUES(?,?,?,?,?)",
                (identifier, name, state, now, released),
            )
    epoch = snapshot.get("inventory", {}).get("epoch", 0)
    store.set_meta("scene_epoch", epoch)
    for row in snapshot.get("inventory", {}).get("items", []):
        point = row["position"]
        state = row["state"]
        if state not in ("PLACED", "COLLECTED"):
            state = "UNKNOWN"
        db.execute(
            """INSERT INTO inventory_items(item_id,sku,frame_id,x,y,z,observed,seen_tick,run_id,epoch,state,verification)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'REVIEW')""",
            (
                row["item_id"],
                row["sku"],
                row["frame_id"],
                *point,
                row.get("observed_unix_sec", now),
                0,
                "legacy",
                epoch,
                state,
            ),
        )
        db.execute(
            "INSERT INTO inventory_observations(item_id,sku,frame_id,x,y,z,observed,run_id,epoch,source) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                row["item_id"],
                row["sku"],
                row["frame_id"],
                *point,
                row.get("observed_unix_sec", now),
                "legacy",
                epoch,
                "legacy_snapshot",
            ),
        )
        owner = row.get("order_id")
        if owner in order_rows:
            db.execute(
                "INSERT INTO reservations(item_id,order_id,state,created,updated) VALUES(?,?,?,?,?)",
                (
                    row["item_id"],
                    owner,
                    "CONSUMED" if state in ("PLACED", "COLLECTED") else "REVIEW",
                    now,
                    now,
                ),
            )
    # Keep known entity evidence, without pretending that live simulator handles survived.
    for allocation in snapshot.get("allocations", []):
        identifier = allocation["order_id"]
        for entity in allocation.get("entities", []):
            sku = entity["sku"]
            name = f"{allocation.get('zone', 'A')}{entity['bin_index'] + 1}"
            bin_id(name)
            source = entity.get("source", [0, 0, 0])
            # Snapshot inventory and allocation evidence may describe the same
            # delivered item. Reuse only an unambiguous consumed record at the
            # same source; never merge a merely nearby, unpicked candidate.
            candidates = list(
                db.execute(
                    """SELECT i.item_id FROM inventory_items i
                   JOIN reservations r ON r.item_id=i.item_id
                   WHERE r.order_id=? AND r.state='CONSUMED' AND i.sku=?
                     AND abs(i.x-?)<0.000001 AND abs(i.y-?)<0.000001
                     AND abs(i.z-?)<0.000001
                     AND NOT EXISTS(SELECT 1 FROM placed_items p WHERE p.item_id=i.item_id)""",
                    (identifier, sku, *source),
                )
            )
            item = (
                candidates[0][0]
                if len(candidates) == 1
                else "legacy-placed-" + uuid.uuid4().hex
            )
            if len(candidates) != 1:
                db.execute(
                    """INSERT INTO inventory_items(item_id,sku,frame_id,x,y,z,observed,seen_tick,run_id,epoch,state,verification)
                           VALUES(?,?,'legacy',?,?,?, ?,0,'legacy',?,'PLACED','REVIEW')""",
                    (item, sku, *source, now, epoch),
                )
            db.execute(
                "INSERT INTO placed_items VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    item,
                    identifier,
                    sku,
                    name,
                    entity["name"],
                    json.dumps(source),
                    json.dumps(source),
                    None,
                    now,
                ),
            )
    store.set_meta("migration", {"from": "legacy_snapshot", "at": now})


def legacy_verified_progress(db, identifier):
    """Migration only. An operation's cumulative JSON never drives new execution."""
    progress = {}
    for row in db.execute(
        "SELECT result FROM operations WHERE order_id=? AND state='VERIFIED'",
        (identifier,),
    ):
        result = json.loads(row[0]) if row[0] else {}
        for sku, count in result.get("completed_by_sku", {}).items():
            if type(count) is int and count >= 0:
                progress[sku] = max(progress.get(sku, 0), count)
    return progress


def upgrade_v2(store):
    """Run under the migration owner's transaction. Preserve historical counts."""
    db = store.db
    db.execute("ALTER TABLE order_lines RENAME COLUMN completed TO legacy_completed")
    for row in list(db.execute("SELECT * FROM order_lines")):
        placed = db.execute(
            "SELECT COUNT(*) FROM placed_items WHERE order_id=? AND sku=?",
            (row["order_id"], row["sku"]),
        ).fetchone()[0]
        old = max(
            row["legacy_completed"],
            legacy_verified_progress(db, row["order_id"]).get(row["sku"], 0),
        )
        if placed > row["quantity"]:
            raise ValueError(
                "Historical placements exceed demand; reconcile before upgrading"
            )
        db.execute(
            "UPDATE order_lines SET legacy_completed=? WHERE order_id=? AND sku=?",
            (max(0, min(row["quantity"], old) - placed), row["order_id"], row["sku"]),
        )
    db.execute("ALTER TABLE inventory_items DROP COLUMN version")
    db.execute("ALTER TABLE reservations ADD COLUMN released REAL")
    db.execute("UPDATE reservations SET released=updated WHERE state='RELEASED'")
    db.execute("""UPDATE inventory_items SET state='UNKNOWN',verification='REVIEW',executing=0
                  WHERE state NOT IN ('PLACED','COLLECTED','RETIRED')
                  AND item_id IN (SELECT item_id FROM reservations WHERE state='REVIEW')""")
    db.execute("DROP INDEX one_item_owner")
    db.execute("DROP INDEX reservation_order")
    db.execute("ALTER TABLE reservations DROP COLUMN state")
    db.execute(
        "CREATE UNIQUE INDEX one_item_owner ON reservations(item_id) WHERE released IS NULL"
    )
    db.execute("CREATE INDEX reservation_order ON reservations(order_id,released)")
    db.execute("CREATE INDEX placed_order_sku ON placed_items(order_id,sku)")
    db.execute("""
        CREATE VIEW order_progress AS
        SELECT l.order_id,l.sku,l.quantity,l.legacy_completed,
            l.legacy_completed+(SELECT COUNT(*) FROM placed_items p
                WHERE p.order_id=l.order_id AND p.sku=l.sku) AS completed
        FROM order_lines l
    """)
    db.execute("""
        CREATE VIEW reservation_status AS
        SELECT r.*, CASE WHEN r.released IS NOT NULL THEN 'RELEASED'
            WHEN i.state='RESERVED' THEN 'HELD'
            WHEN i.state='CARRYING' THEN 'CARRYING'
            WHEN i.state IN ('PLACED','COLLECTED') THEN 'CONSUMED'
            ELSE 'REVIEW' END AS state
        FROM reservations r JOIN inventory_items i USING(item_id)
    """)
    db.execute("""
        CREATE TRIGGER placement_within_demand BEFORE INSERT ON placed_items
        WHEN EXISTS(SELECT 1 FROM order_progress l WHERE l.order_id=NEW.order_id
                    AND l.sku=NEW.sku AND l.completed>=l.quantity)
        BEGIN SELECT RAISE(ABORT,'Placement exceeds remaining demand'); END
    """)
    # Empty bootstrap history is unnecessary; nonempty pre-upgrade history is preserved.
    if not db.execute("SELECT 1 FROM inventory_observations LIMIT 1").fetchone():
        db.execute("DROP TABLE inventory_observations")
    for table, column, redundant in (
        ("events", "body", ("order_id", "stage")),
        ("requests", "body", ("request_id", "expected_database_version")),
        ("operations", "result", ("completed_by_sku",)),
    ):
        for row in list(
            db.execute(f"SELECT rowid,{column} FROM {table} WHERE {column} IS NOT NULL")
        ):
            payload = json.loads(row[1])
            if isinstance(payload, dict) and any(key in payload for key in redundant):
                for key in redundant:
                    payload.pop(key, None)
                db.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (json.dumps(payload, ensure_ascii=False), row[0]),
                )
    db.execute("PRAGMA user_version=2")
