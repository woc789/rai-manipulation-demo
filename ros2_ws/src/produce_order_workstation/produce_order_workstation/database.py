"""Single SQLite business store. No runtime JSON snapshot or second state cache."""

from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
from threading import RLock
import time
import uuid


class WorkcellDatabase:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._depth = 0
        self.session_id = uuid.uuid4().hex
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                phase TEXT NOT NULL, state TEXT NOT NULL, result TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, order_id TEXT, stage TEXT, body TEXT, created REAL);
        """)
        try:
            self._migrate()
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def read_transaction(self):
        """One coherent read across tables; no write lock or snapshot persistence."""
        with self._lock:
            if self._depth:
                yield self.db
                return
            self.db.execute("BEGIN")
            self._depth = 1
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            finally:
                self._depth = 0

    @contextmanager
    def transaction(self):
        """Short write unit; nested repository calls use savepoints, never commit the owner."""
        with self._lock:
            level = self._depth
            savepoint = f"nested_{level}"
            self.db.execute(
                "BEGIN IMMEDIATE" if not level else f"SAVEPOINT {savepoint}"
            )
            self._depth += 1
            try:
                yield self.db
                self.db.execute(
                    "COMMIT" if not level else f"RELEASE SAVEPOINT {savepoint}"
                )
            except BaseException:
                if self.db.in_transaction:
                    if not level:
                        self.db.rollback()
                    else:
                        self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._depth -= 1

    def rows(self, sql, parameters=()):
        with self._lock:
            return list(self.db.execute(sql, parameters))

    def meta(self, key, default=None):
        rows = self.rows("SELECT value FROM metadata WHERE key=?", (key,))
        return json.loads(rows[0][0]) if rows else default

    def set_meta(self, key, value):
        with self.transaction() as db:
            db.execute(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise ValueError("Database schema is newer than this application")
        if version == 1:
            return
        has_legacy = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='snapshots' AND type='table'"
        ).fetchone()
        legacy = (
            self.db.execute("SELECT body FROM snapshots WHERE id=1").fetchone()
            if has_legacy
            else None
        )
        if legacy and str(self.path) != ":memory:":
            backup_path = self.path.with_name(self.path.name + ".pre-relational.bak")
            if backup_path.exists():
                raise ValueError(
                    f"Migration backup already exists; inspect it before retrying: {backup_path}"
                )
            with sqlite3.connect(backup_path) as backup:
                self.db.backup(backup)
        from .schema import SCHEMA, import_legacy

        with self.transaction() as db:
            for statement in SCHEMA:
                db.execute(statement)
            if legacy:
                import_legacy(self, json.loads(legacy[0]))
            db.execute("DROP TABLE IF EXISTS snapshots")
            db.execute("PRAGMA user_version=1")

    def event(self, entry):
        with self.transaction():
            self.db.execute(
                "INSERT INTO events(order_id,stage,body,created) VALUES(?,?,?,?)",
                (
                    entry.get("order_id"),
                    entry.get("stage"),
                    json.dumps(entry, ensure_ascii=False),
                    time.time(),
                ),
            )

    def begin_operation(self, order_id, phase, operation_id=None):
        identifier = operation_id or uuid.uuid4().hex
        with self.transaction():
            row = self.db.execute(
                "SELECT state FROM operations WHERE operation_id=?", (identifier,)
            ).fetchone()
            if row:
                raise ValueError(
                    "Operation ID already exists; never replay physical motion"
                )
            self.db.execute(
                "INSERT INTO operations VALUES(?,?,?,'INTENT',NULL,?)",
                (identifier, order_id, phase, time.time()),
            )
        return identifier

    def events_for_order(self, order_id):
        return [
            json.loads(row["body"])
            for row in self.rows(
                "SELECT body FROM events WHERE order_id=? ORDER BY id", (order_id,)
            )
        ]

    def finish_operation(self, identifier, state, result=None):
        with self.transaction():
            changed = self.db.execute(
                "UPDATE operations SET state=?,result=?,updated=? "
                "WHERE operation_id=? AND state='INTENT'",
                (
                    state,
                    json.dumps(result, ensure_ascii=False),
                    time.time(),
                    identifier,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("Operation result already committed or unknown")

    def operations(self):
        with self._lock:
            return [
                dict(operation_id=r[0], order_id=r[1], phase=r[2], state=r[3])
                for r in self.db.execute(
                    "SELECT operation_id,order_id,phase,state FROM operations"
                )
            ]

    def operation(self, identifier):
        with self._lock:
            row = self.db.execute(
                "SELECT order_id,phase,state,result FROM operations WHERE operation_id=?",
                (identifier,),
            ).fetchone()
            return (
                dict(
                    order_id=row[0],
                    phase=row[1],
                    state=row[2],
                    result=json.loads(row[3]) if row[3] else None,
                )
                if row
                else None
            )

    def verified_progress(self, order_id):
        with self._lock:
            progress = {}
            for (encoded,) in self.db.execute(
                "SELECT result FROM operations WHERE order_id=? AND state='VERIFIED'",
                (order_id,),
            ):
                result = json.loads(encoded) if encoded else {}
                for sku, count in result.get("completed_by_sku", {}).items():
                    if type(count) is int and count >= 0:
                        progress[sku] = max(progress.get(sku, 0), count)
            return progress

    def request(self, identifier):
        with self._lock:
            row = self.db.execute(
                "SELECT body,state FROM requests WHERE request_id=?", (identifier,)
            ).fetchone()
            return dict(json.loads(row[0]), state=row[1]) if row else None

    def quarantine_interrupted_maintenance(self):
        with self.transaction():
            interrupted = False
            rows = list(
                self.db.execute(
                    "SELECT request_id,body FROM requests WHERE state='APPROVED'"
                )
            )
            for identifier, encoded in rows:
                body = json.loads(encoded)
                if body.get("execution_state") in ("ADMITTING", "RUNNING"):
                    interrupted = True
                    body.update(
                        execution_state="UNKNOWN",
                        execution_error="Process restarted before maintenance result; reconcile scene",
                    )
                    self.db.execute(
                        "UPDATE requests SET body=? WHERE request_id=?",
                        (json.dumps(body), identifier),
                    )
            return interrupted

    def admit_maintenance(self, identifier):
        """Durably admit once; only a definite pre-execution rejection is retryable."""
        with self.transaction():
            request = self.request(identifier)
            if not request or not (
                request["state"] == "PENDING"
                or (
                    request["state"] == "APPROVED"
                    and request.get("execution_state") == "REJECTED"
                )
            ):
                raise ValueError(
                    "Maintenance already admitted or result unknown; do not repeat"
                )
            request.pop("state")
            request.update(execution_state="ADMITTING", execution_error="")
            self.db.execute(
                "UPDATE requests SET state='APPROVED',body=? WHERE request_id=?",
                (json.dumps(request, ensure_ascii=False), identifier),
            )

    def execution_result(
        self, identifier, state, error="", *, expected=("ADMITTING", "RUNNING")
    ):
        if state not in ("REJECTED", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"):
            raise ValueError("Invalid maintenance execution state")
        with self.transaction():
            request = self.request(identifier)
            if not request or request.get("execution_state") not in expected:
                return False
            request.pop("state")
            request.update(execution_state=state, execution_error=error)
            self.db.execute(
                "UPDATE requests SET body=? WHERE request_id=?",
                (json.dumps(request, ensure_ascii=False), identifier),
            )
            return True

    def create_request(self, body):
        identifier = uuid.uuid4().hex
        with self.transaction():
            for (encoded,) in self.db.execute(
                "SELECT body FROM requests WHERE state='PENDING'"
            ):
                existing = json.loads(encoded)
                original = {
                    k: v
                    for k, v in existing.items()
                    if k not in ("request_id", "created", "expires")
                }
                if original == body and existing["expires"] > time.time():
                    return existing
            body = dict(
                body,
                request_id=identifier,
                created=time.time(),
                expires=time.time() + 600,
            )
            self.db.execute(
                "INSERT INTO requests VALUES(?,?,'PENDING')",
                (identifier, json.dumps(body, ensure_ascii=False)),
            )
        return body

    def requests(self):
        with self._lock:
            return [
                dict(json.loads(body), state=state)
                for body, state in self.db.execute(
                    "SELECT body,state FROM requests ORDER BY rowid DESC LIMIT 50"
                )
            ]

    def resolve_request(self, identifier, state):
        with self.transaction():
            if (
                self.db.execute(
                    "UPDATE requests SET state=? WHERE request_id=? AND state='PENDING'",
                    (state, identifier),
                ).rowcount
                != 1
            ):
                raise ValueError("Request was already resolved or does not exist")

    def close(self):
        with self._lock:
            self.db.close()
