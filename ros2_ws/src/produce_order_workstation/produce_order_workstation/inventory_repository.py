"""Database-backed visual inventory. Per-call records are discarded after each short transaction."""

from dataclasses import dataclass
import json
from math import dist, isfinite
import time
import uuid


@dataclass
class VisualRecord:
    item_id: str
    sku: str
    position: tuple
    frame_id: str
    observed_unix_sec: float
    seen_tick: float
    run_id: str
    epoch: int
    state: str = "AVAILABLE"
    order_id: str = ""
    executing: bool = False
    verification: str = "FRESH"


class InventoryRepository:
    def __init__(
        self,
        store,
        association_radius=0.035,
        ttl=60.0,
        *,
        clock=time.monotonic,
        wall_clock=time.time,
        run_id=None,
        record_observations=False,
        observation_limit=1000,
    ):
        if association_radius <= 0 or ttl <= 0:
            raise ValueError("Inventory radius and TTL must be positive")
        self.store, self.radius, self.ttl = store, association_radius, ttl
        self.clock, self.wall_clock = clock, wall_clock
        self.run_id = run_id or store.session_id
        if type(observation_limit) is not int or observation_limit < 1:
            raise ValueError("Observation limit must be positive")
        self.record_observations = bool(record_observations)
        self.observation_limit = observation_limit
        if self.record_observations:
            from .schema import OBSERVATION_SCHEMA

            with store.transaction() as db:
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='inventory_observations'"
                ).fetchone():
                    db.execute(OBSERVATION_SCHEMA)
                    db.execute(
                        "CREATE INDEX observations_item ON inventory_observations(item_id,observed)"
                    )

    @property
    def epoch(self):
        return self.store.meta("scene_epoch", 0)

    def _read(self):
        records = {}
        for row in self.store.rows("""SELECT i.*,r.order_id FROM inventory_items i LEFT JOIN reservations r
            ON r.item_id=i.item_id AND r.released IS NULL WHERE i.state<>'RETIRED' ORDER BY i.rowid"""):
            item = VisualRecord(
                row["item_id"],
                row["sku"],
                (row["x"], row["y"], row["z"]),
                row["frame_id"],
                row["observed"],
                row["seen_tick"],
                row["run_id"],
                row["epoch"],
                row["state"],
                row["order_id"] or "",
                bool(row["executing"]),
                row["verification"],
            )
            if (
                not item.executing
                and item.state in ("AVAILABLE", "RESERVED")
                and (
                    item.run_id != self.run_id
                    or self.clock() - item.seen_tick > self.ttl
                )
            ):
                item.state, item.verification = "UNKNOWN", "STALE"
            records[item.item_id] = item
        return records

    def _write(self, records):
        """Must run within store.transaction; no observer can see half a reservation."""
        db = self.store.db
        now = self.wall_clock()
        for item in records.values():
            values = (
                item.sku,
                item.frame_id,
                *item.position,
                item.observed_unix_sec,
                item.seen_tick,
                item.run_id,
                item.epoch,
                item.state,
                item.verification,
                int(item.executing),
            )
            old = db.execute(
                "SELECT * FROM inventory_items WHERE item_id=?", (item.item_id,)
            ).fetchone()
            old_values = (
                None
                if old is None
                else tuple(
                    old[key]
                    for key in (
                        "sku",
                        "frame_id",
                        "x",
                        "y",
                        "z",
                        "observed",
                        "seen_tick",
                        "run_id",
                        "epoch",
                        "state",
                        "verification",
                        "executing",
                    )
                )
            )
            if old is None:
                db.execute(
                    """INSERT INTO inventory_items(sku,frame_id,x,y,z,observed,seen_tick,run_id,epoch,state,verification,executing,item_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, item.item_id),
                )
            elif values != old_values:
                db.execute(
                    """UPDATE inventory_items SET sku=?,frame_id=?,x=?,y=?,z=?,observed=?,seen_tick=?,run_id=?,epoch=?,
                    state=?,verification=?,executing=? WHERE item_id=?""",
                    (*values, item.item_id),
                )
            reservation = db.execute(
                "SELECT * FROM reservations WHERE item_id=? AND released IS NULL",
                (item.item_id,),
            ).fetchone()
            if reservation and reservation["order_id"] != item.order_id:
                db.execute(
                    "UPDATE reservations SET released=?,updated=? WHERE reservation_id=?",
                    (now, now, reservation["reservation_id"]),
                )
                reservation = None
            if item.order_id:
                if reservation is None:
                    db.execute(
                        "INSERT INTO reservations(item_id,order_id,created,updated) VALUES(?,?,?,?)",
                        (item.item_id, item.order_id, now, now),
                    )

    def reset(self):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE reservations SET released=?,updated=? WHERE released IS NULL",
                (self.wall_clock(), self.wall_clock()),
            )
            db.execute(
                "UPDATE inventory_items SET state='RETIRED',executing=0 WHERE state<>'RETIRED'"
            )
            self.store.set_meta("scene_epoch", self.epoch + 1)

    def invalidate_source(self):
        with self.store.transaction() as db:
            db.execute(
                """UPDATE reservations SET released=?,updated=? WHERE released IS NULL
                AND item_id IN (SELECT item_id FROM inventory_items WHERE state NOT IN ('PLACED','COLLECTED'))""",
                (self.wall_clock(), self.wall_clock()),
            )
            db.execute(
                "UPDATE inventory_items SET state='RETIRED',executing=0 WHERE state NOT IN ('PLACED','COLLECTED')"
            )
            self.store.set_meta("scene_epoch", self.epoch + 1)

    def observe(self, sku, frame_id, points, *, complete_scan=True):
        points = [tuple(float(value) for value in point) for point in points]
        if (
            not sku
            or not frame_id
            or any(len(p) != 3 or not all(isfinite(v) for v in p) for p in points)
        ):
            raise ValueError("Invalid visual observation position or frame")
        with self.store.transaction() as db:
            items = self._read()
            previous = [
                item
                for item in items.values()
                if item.sku == sku
                and item.frame_id == frame_id
                and item.run_id == self.run_id
                and item.state in ("AVAILABLE", "UNKNOWN", "RESERVED")
            ]
            used = set()
            identities = []
            for point in points:
                matches = [
                    item
                    for item in previous
                    if item.item_id not in used
                    and dist(item.position[:2], point[:2]) <= self.radius
                    and abs(item.position[2] - point[2]) <= self.radius
                ]
                item = (
                    min(matches, key=lambda candidate: dist(candidate.position, point))
                    if matches
                    else None
                )
                if item is None:
                    item = VisualRecord(
                        "obs-" + uuid.uuid4().hex,
                        sku,
                        point,
                        frame_id,
                        self.wall_clock(),
                        self.clock(),
                        self.run_id,
                        self.epoch,
                    )
                    items[item.item_id] = item
                item.position = point
                item.observed_unix_sec, item.seen_tick = self.wall_clock(), self.clock()
                item.state, item.verification = (
                    ("RESERVED" if item.order_id else "AVAILABLE"),
                    "FRESH",
                )
                used.add(item.item_id)
                identities.append(item.item_id)
            if complete_scan:
                unmatched = [item for item in previous if item.item_id not in used]
                for item in unmatched:
                    item.state, item.verification = "UNKNOWN", "UNSEEN"
                owned = [item for item in unmatched if item.order_id]
                for identity in identities:
                    item = items[identity]
                    if not item.order_id and any(
                        dist(item.position[:2], old.position[:2]) <= 2 * self.radius
                        for old in owned
                    ):
                        item.state, item.verification = "UNKNOWN", "AMBIGUOUS"
            for identity in identities:
                item = items[identity]
                for other in items.values():
                    if (
                        other.item_id != identity
                        and other.sku != sku
                        and other.frame_id == frame_id
                        and self._fresh(other)
                        and other.state in ("AVAILABLE", "RESERVED", "UNKNOWN")
                        and self._near(other.position, item.position)
                    ):
                        item.state = other.state = "UNKNOWN"
                        item.verification = other.verification = "AMBIGUOUS"
            self._write(items)
            if self.record_observations:
                for identity in identities:
                    item = items[identity]
                    db.execute(
                        """INSERT INTO inventory_observations(item_id,sku,frame_id,x,y,z,observed,run_id,epoch,source)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            identity,
                            sku,
                            frame_id,
                            *item.position,
                            item.observed_unix_sec,
                            self.run_id,
                            self.epoch,
                            "rgbd_detection_segmentation_depth_tf",
                        ),
                    )
                db.execute(
                    "DELETE FROM inventory_observations WHERE observation_id NOT IN "
                    "(SELECT observation_id FROM inventory_observations ORDER BY observation_id DESC LIMIT ?)",
                    (self.observation_limit,),
                )
            return identities

    def _fresh(self, item):
        return item.run_id == self.run_id and self.clock() - item.seen_tick <= self.ttl

    def _near(self, left, right):
        return (
            dist(left[:2], right[:2]) <= self.radius
            and abs(left[2] - right[2]) <= self.radius
        )

    def reserve(self, item_id, order_id, refreshed_position=None):
        with self.store.transaction():
            items = self._read()
            item = items[item_id]
            if (
                refreshed_position is not None
                and item.state in ("AVAILABLE", "UNKNOWN", "RESERVED")
                and item.order_id in ("", order_id)
            ):
                point = tuple(float(v) for v in refreshed_position)
                if (
                    len(point) != 3
                    or not all(isfinite(v) for v in point)
                    or dist(item.position[:2], point[:2]) > self.radius
                ):
                    raise ValueError(
                        "Invalid refreshed visual position or moved beyond association gate"
                    )
                item.position = point
                item.observed_unix_sec, item.seen_tick = self.wall_clock(), self.clock()
                item.run_id = self.run_id
                item.verification = "FRESH"
                item.state = "RESERVED" if item.order_id else "AVAILABLE"
            if item.state == "RESERVED" and item.order_id == order_id:
                self._write(items)
                return
            if not order_id or item.state != "AVAILABLE" or item.order_id:
                raise ValueError("Visual item is unavailable or already reserved")
            item.state, item.order_id = "RESERVED", order_id
            self._write(items)

    def selectable(self, item_id, order_id):
        with self.store._lock:
            item = self._read()[item_id]
            return item.state == "AVAILABLE" or (
                item.state == "RESERVED" and item.order_id == order_id
            )

    def conflicting_labels(self, item_id):
        with self.store._lock:
            items = self._read()
            item = items[item_id]
            return sorted(
                {
                    other.sku
                    for other in items.values()
                    if other.item_id != item_id
                    and other.sku != item.sku
                    and other.frame_id == item.frame_id
                    and self._fresh(other)
                    and other.state in ("AVAILABLE", "RESERVED", "UNKNOWN")
                    and self._near(item.position, other.position)
                }
            )

    def ambiguous_skus(self):
        with self.store._lock:
            return {
                item.sku
                for item in self._read().values()
                if item.state == "UNKNOWN"
                and self._fresh(item)
                and self.conflicting_labels(item.item_id)
            }

    def select_for_order(self, item_id, order):
        with self.store.transaction():
            items = self._read()
            item = items[item_id]
            demand = {line.sku: line.quantity for line in order.lines}
            if item.executing:
                raise ValueError("Candidate already has a physical action in flight")
            if item.sku not in demand or not self.selectable(item_id, order.order_id):
                raise ValueError("Candidate is not available for this order")
            if item.order_id == order.order_id:
                return
            if any(
                other.item_id != item_id
                and other.order_id
                and other.state in ("RESERVED", "CARRYING", "UNKNOWN")
                and dist(other.position[:2], item.position[:2]) < self.radius
                for other in items.values()
            ):
                raise ValueError("Candidate overlaps another locked physical identity")
            owned = [
                other
                for other in items.values()
                if other.sku == item.sku
                and other.order_id == order.order_id
                and other.state in ("RESERVED", "CARRYING", "PLACED", "UNKNOWN")
            ]
            if len(owned) >= demand[item.sku]:
                replaceable = next(
                    (
                        other
                        for other in owned
                        if other.state == "RESERVED" and not other.executing
                    ),
                    None,
                )
                if replaceable is None:
                    raise ValueError("Order has no untouched reservation to exchange")
                replaceable.state, replaceable.order_id = "AVAILABLE", ""
            item.state, item.order_id = "RESERVED", order.order_id
            self._write(items)

    def reject_candidate(self, item_id, order_id):
        with self.store.transaction():
            items = self._read()
            item = items[item_id]
            if item.state not in ("AVAILABLE", "RESERVED") or item.order_id not in (
                "",
                order_id,
            ):
                raise ValueError("Cannot invalidate another order's or a carried item")
            item.state, item.order_id, item.executing, item.verification = (
                "UNKNOWN",
                "",
                False,
                "REJECTED",
            )
            self._write(items)

    def reserve_order(self, order, *, excluded_item_ids=(), dry_run=False):
        with self.store.transaction():
            items = self._read()
            excluded = set(excluded_item_ids)
            for item in items.values():
                if (
                    item.item_id in excluded
                    and item.order_id == order.order_id
                    and item.state == "RESERVED"
                    and not item.executing
                ):
                    item.state, item.order_id = "AVAILABLE", ""
            selected = []
            missing = {}
            blocked = [
                item.position
                for item in items.values()
                if item.order_id and item.state in ("RESERVED", "CARRYING", "UNKNOWN")
            ]
            for line in order.lines:
                owned = [
                    item
                    for item in items.values()
                    if item.order_id == order.order_id
                    and item.sku == line.sku
                    and item.state in ("RESERVED", "CARRYING", "PLACED")
                ]
                needed = max(0, line.quantity - len(owned))
                chosen = []
                for item in items.values():
                    if len(chosen) >= needed:
                        break
                    if (
                        item.sku != line.sku
                        or item.state != "AVAILABLE"
                        or item.order_id
                        or item.item_id in excluded
                    ):
                        continue
                    if any(
                        dist(item.position[:2], point[:2]) < self.radius
                        for point in blocked
                    ):
                        continue
                    chosen.append(item)
                    blocked.append(item.position)
                if len(chosen) < needed:
                    missing[line.sku] = needed - len(chosen)
                selected.extend(chosen)
            if missing:
                return [], missing
            for item in selected:
                item.state, item.order_id = "RESERVED", order.order_id
            if not dry_run:
                self._write(items)
            return [
                item.item_id
                for item in items.values()
                if item.order_id == order.order_id and item.state == "RESERVED"
            ], {}

    def release_order(self, order_id):
        with self.store.transaction():
            items = self._read()
            for item in items.values():
                if (
                    item.order_id == order_id
                    and item.state == "RESERVED"
                    and not item.executing
                ):
                    item.state, item.order_id = "AVAILABLE", ""
                elif item.order_id == order_id and item.state == "UNKNOWN":
                    item.order_id = ""
            self._write(items)

    def transition(self, item_id, order_id, state):
        with self.store.transaction():
            items = self._read()
            item = items[item_id]
            if (
                item.order_id != order_id
                or item.state != "RESERVED"
                or state != "CARRYING"
            ):
                raise ValueError(
                    "Invalid inventory transition; placement requires verified commit"
                )
            item.state = state
            self._write(items)

    def begin_execution(self, item_id, order_id):
        with self.store.transaction():
            items = self._read()
            item = items[item_id]
            if item.state != "RESERVED" or item.order_id != order_id:
                raise ValueError("Execution requires a fresh owned reservation")
            item.executing = True
            self._write(items)

    def has_active_execution(self):
        return bool(self.store.rows("SELECT 1 FROM inventory_items WHERE executing=1"))

    def finish_failed(self, order_id, motion_attempted):
        with self.store.transaction():
            items = self._read()
            for item in items.values():
                if item.order_id != order_id:
                    continue
                item.executing = False
                if item.state in ("RESERVED", "CARRYING"):
                    item.state = "UNKNOWN" if motion_attempted else "AVAILABLE"
                    item.verification = (
                        "REVIEW" if motion_attempted else item.verification
                    )
                    if not motion_attempted:
                        item.order_id = ""
            self._write(items)

    def collect(self, order_id):
        with self.store.transaction():
            items = self._read()
            for item in items.values():
                if item.order_id == order_id and item.state == "PLACED":
                    item.state = "COLLECTED"
            self._write(items)

    def record_placement(
        self,
        order,
        item_id,
        entity_name,
        bin_id,
        source,
        target,
        operation_id,
        detection_id,
    ):
        """Physical verification happened; commit evidence, stock and quantity together."""
        with self.store.transaction() as db:
            operation = self.store.operation(operation_id)
            if (
                not operation
                or operation["order_id"] != order.order_id
                or operation["phase"] != "PICK_AND_PLACE"
                or operation["state"] != "INTENT"
            ):
                raise ValueError(
                    "Placement requires this order's uncommitted pick intent"
                )
            if self.store.rows(
                "SELECT 1 FROM placed_items WHERE item_id=?", (item_id,)
            ):
                raise ValueError("Placement already recorded; never increment twice")
            items = self._read()
            item = items[item_id]
            if item.order_id != order.order_id or item.state != "CARRYING":
                raise ValueError("Placement requires the carrying reservation")
            if not db.execute(
                "SELECT 1 FROM bin_allocations WHERE order_id=? AND bin_id=? AND released IS NULL AND state='ALLOCATED'",
                (order.order_id, bin_id),
            ).fetchone():
                raise ValueError("Placement bin is not owned by this order")
            demand = db.execute(
                "SELECT completed,quantity FROM order_progress WHERE order_id=? AND sku=?",
                (order.order_id, item.sku),
            ).fetchone()
            if not demand or demand["completed"] >= demand["quantity"]:
                raise ValueError("Placement exceeds remaining demand")
            db.execute(
                "INSERT INTO placed_items VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    order.order_id,
                    item.sku,
                    bin_id,
                    entity_name,
                    json.dumps(source),
                    json.dumps(target),
                    operation_id,
                    self.wall_clock(),
                ),
            )
            item.state, item.executing = "PLACED", False
            self._write(items)
            progress = {
                row["sku"]: row["completed"]
                for row in db.execute(
                    "SELECT sku,completed FROM order_progress WHERE order_id=?",
                    (order.order_id,),
                )
            }
            result = dict(
                status="SUCCESS",
                reason="",
                remaining=order.total_items - sum(progress.values()),
                detection_id="",
                request_detection_id=detection_id,
            )
            self.store.finish_operation(operation_id, "VERIFIED", result)
            db.execute(
                "UPDATE orders SET updated=?,version=version+1 WHERE order_id=?",
                (self.wall_clock(), order.order_id),
            )
            return progress

    def snapshot(self):
        with self.store.read_transaction():
            rows = []
            for item in self._read().values():
                age = (
                    max(0.0, self.clock() - item.seen_tick)
                    if item.run_id == self.run_id
                    else max(self.ttl + 1, self.wall_clock() - item.observed_unix_sec)
                )
                rows.append(
                    dict(
                        item_id=item.item_id,
                        sku=item.sku,
                        position=list(item.position),
                        frame_id=item.frame_id,
                        observed_unix_sec=item.observed_unix_sec,
                        state=item.state,
                        order_id=item.order_id,
                        age_sec=age,
                        stale=age > self.ttl,
                        verification=item.verification,
                        classification_verified=False,
                        observation_source="rgbd_detection_segmentation_depth_tf",
                    )
                )
            return dict(epoch=self.epoch, persistence="sqlite", items=rows)

    def summary(self):
        result = {}
        for item in self.snapshot()["items"]:
            states = result.setdefault(item["sku"], {})
            states[item["state"]] = states.get(item["state"], 0) + 1
        return result
