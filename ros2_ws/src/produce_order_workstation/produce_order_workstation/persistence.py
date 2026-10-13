"""Short SQLite transactions; durable evidence is never proof of physical state."""

import json
import sqlite3
from pathlib import Path
from threading import RLock
import time
import uuid


class WorkcellStore:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                phase TEXT NOT NULL, state TEXT NOT NULL, result TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, order_id TEXT, stage TEXT, body TEXT, created REAL);
        """)
        self._last = None

    def load(self):
        with self._lock:
            row = self.db.execute("SELECT body FROM snapshots WHERE id=1").fetchone()
            return json.loads(row[0]) if row else None

    def save(self, snapshot, *, resolution=None):
        body = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        with self._lock:
            if body == self._last and resolution is None:
                return
            with self.db:
                self.db.execute("INSERT INTO snapshots VALUES(1,?,?) ON CONFLICT(id) "
                                "DO UPDATE SET body=excluded.body,updated=excluded.updated",
                                (body, time.time()))
                if resolution:
                    identifier, state = resolution
                    if self.db.execute("UPDATE requests SET state=? WHERE request_id=? AND state='PENDING'",
                                       (state, identifier)).rowcount != 1:
                        raise ValueError("Request was already resolved; snapshot transaction rolled back")
            self._last = body

    def event(self, entry):
        with self._lock, self.db:
            self.db.execute("INSERT INTO events(order_id,stage,body,created) VALUES(?,?,?,?)",
                            (entry.get("order_id"), entry.get("stage"),
                             json.dumps(entry, ensure_ascii=False), time.time()))

    def begin_operation(self, order_id, phase, operation_id=None):
        identifier = operation_id or uuid.uuid4().hex
        with self._lock, self.db:
            row = self.db.execute("SELECT state FROM operations WHERE operation_id=?", (identifier,)).fetchone()
            if row:
                raise ValueError("Operation ID already exists; never replay physical motion")
            self.db.execute("INSERT INTO operations VALUES(?,?,?,'INTENT',NULL,?)",
                            (identifier, order_id, phase, time.time()))
        return identifier

    def finish_operation(self, identifier, state, result=None):
        with self._lock, self.db:
            changed = self.db.execute("UPDATE operations SET state=?,result=?,updated=? "
                                     "WHERE operation_id=? AND state='INTENT'",
                                     (state, json.dumps(result, ensure_ascii=False), time.time(), identifier)).rowcount
            if changed != 1:
                raise ValueError("Operation result already committed or unknown")

    def operations(self):
        with self._lock:
            return [dict(operation_id=r[0], order_id=r[1], phase=r[2], state=r[3])
                    for r in self.db.execute("SELECT operation_id,order_id,phase,state FROM operations")]

    def create_request(self, body):
        identifier = uuid.uuid4().hex
        with self._lock, self.db:
            for encoded, in self.db.execute("SELECT body FROM requests WHERE state='PENDING'"):
                existing = json.loads(encoded)
                original = {k: v for k, v in existing.items() if k not in ("request_id", "created", "expires")}
                if original == body and existing["expires"] > time.time():
                    return existing
            body = dict(body, request_id=identifier, created=time.time(), expires=time.time()+600)
            self.db.execute("INSERT INTO requests VALUES(?,?,'PENDING')", (identifier, json.dumps(body, ensure_ascii=False)))
        return body

    def requests(self):
        with self._lock:
            return [dict(json.loads(body), state=state)
                    for body, state in self.db.execute("SELECT body,state FROM requests ORDER BY rowid DESC LIMIT 50")]

    def resolve_request(self, identifier, state):
        with self._lock, self.db:
            if self.db.execute("UPDATE requests SET state=? WHERE request_id=? AND state='PENDING'",
                               (state, identifier)).rowcount != 1:
                raise ValueError("Request was already resolved or does not exist")

    def close(self):
        with self._lock:
            self.db.close()
