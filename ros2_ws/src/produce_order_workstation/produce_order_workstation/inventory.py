"""Thread-safe visual candidate ledger. Never seeded with simulator truth.

Counts describe observations, not guaranteed stock. Identity is approximate
same-SKU spatial association; overlapping cross-SKU labels are quarantined.
All motion safety checks remain in Workstation.
"""

from dataclasses import dataclass, asdict
from contextlib import contextmanager
from copy import deepcopy
from math import dist, isfinite
from threading import RLock
import time


@dataclass
class InventoryItem:
    item_id: str
    sku: str
    position: tuple[float, float, float]
    frame_id: str
    observed_unix_sec: float
    seen_monotonic: float
    state: str = "AVAILABLE"
    order_id: str = ""
    classification_verified: bool = False
    observation_source: str = "rgbd_detection_segmentation_depth_tf"


class InventoryLedger:
    """Visual identities and atomic order reservations, without durable recovery."""

    def __init__(self, association_radius=0.035, ttl=60.0, *, clock=time.monotonic, wall_clock=time.time):
        if association_radius <= 0 or ttl <= 0:
            raise ValueError("Inventory radius and TTL must be positive")
        self.radius, self.ttl = association_radius, ttl
        self.clock, self.wall_clock = clock, wall_clock
        self.epoch = 0
        self._serial = 0
        self._items = {}
        self._executing = set()
        self._lock = RLock()

    def reset(self):
        with self._lock:
            self._items.clear()
            self._executing.clear()
            self.epoch += 1

    @contextmanager
    def transaction(self):
        """Keep observers out; roll back reservations if a business commit fails."""
        with self._lock:
            before = (deepcopy(self._items), set(self._executing), self.epoch, self._serial)
            try:
                yield
            except BaseException:
                self._items, self._executing, self.epoch, self._serial = before
                raise

    def observe(self, sku, frame_id, points, *, complete_scan=True):
        if not sku or not frame_id:
            raise ValueError("Observation needs SKU and frame")
        points = [tuple(float(v) for v in point) for point in points]
        if any(len(point) != 3 or not all(isfinite(v) for v in point) for point in points):
            raise ValueError("Invalid visual observation position")
        with self._lock:
            self._expire()
            previous = [item for item in self._items.values()
                        if item.sku == sku and item.frame_id == frame_id
                        and item.state in ("AVAILABLE", "UNKNOWN", "RESERVED")]
            used, identities = set(), []
            for point in points:
                matches = [item for item in self._items.values()
                           if item.sku == sku and item.frame_id == frame_id and item.item_id not in used
                           and item.state in ("AVAILABLE", "UNKNOWN", "RESERVED")
                           and dist(item.position[:2], point[:2]) <= self.radius
                           and abs(item.position[2] - point[2]) <= self.radius]
                item = min(matches, key=lambda item: dist(item.position, point)) if matches else None
                if item is None:
                    self._serial += 1
                    item = InventoryItem(f"obs-{self.epoch}-{self._serial}", sku, point, frame_id,
                                         self.wall_clock(), self.clock())
                    self._items[item.item_id] = item
                item.position = point
                item.observed_unix_sec, item.seen_monotonic = self.wall_clock(), self.clock()
                item.state = "RESERVED" if item.order_id else "AVAILABLE"
                used.add(item.item_id)
                identities.append(item.item_id)
            if complete_scan:
                unmatched = [item for item in previous if item.item_id not in used]
                for item in unmatched:
                    # Retain history/ownership, but do not promise an old location
                    # alongside a new candidate that may be the same moved object.
                    item.state = "UNKNOWN"
                uncertain_owned = [item for item in unmatched if item.order_id]
                if uncertain_owned:
                    # Quarantine local identity ambiguity, not unrelated stock
                    # across the entire table. A fresh position beyond the
                    # association gate is never silently transferred to an owner.
                    for identity in identities:
                        item = self._items[identity]
                        if not item.order_id and any(
                            dist(item.position[:2], old.position[:2]) <= 2 * self.radius
                            for old in uncertain_owned
                        ):
                            self._items[identity].state = "UNKNOWN"
            # Independent text prompts can label the same RGB-D point as two
            # products. Neither label is trustworthy stock until a different
            # observation resolves the conflict. Never release an order owner.
            for identity in identities:
                item = self._items[identity]
                conflicts = [other for other in self._items.values()
                             if other.item_id != identity and other.sku != sku
                             and other.frame_id == frame_id
                             and other.state in ("AVAILABLE", "RESERVED", "UNKNOWN")
                             and self.clock() - other.seen_monotonic <= self.ttl
                             and dist(other.position[:2], item.position[:2]) <= self.radius
                             and abs(other.position[2] - item.position[2]) <= self.radius]
                if conflicts:
                    item.state = "UNKNOWN"
                    for other in conflicts:
                        other.state = "UNKNOWN"
            # An empty scan means uncertainty, not deletion or proven zero stock.
            return identities

    def reserve(self, item_id, order_id, refreshed_position=None):
        with self._lock:
            self._expire()
            item = self._items[item_id]
            if refreshed_position is not None and item.state in ("AVAILABLE", "UNKNOWN", "RESERVED") and item.order_id in ("", order_id):
                position = tuple(float(v) for v in refreshed_position)
                if len(position) != 3 or not all(isfinite(v) for v in position):
                    raise ValueError("Invalid refreshed visual position")
                if dist(item.position[:2], position[:2]) > self.radius:
                    raise ValueError("Refreshed candidate moved beyond association gate")
                item.position = position
                item.observed_unix_sec, item.seen_monotonic = self.wall_clock(), self.clock()
                item.state = "RESERVED" if item.order_id else "AVAILABLE"
            if item.state == "RESERVED" and item.order_id == order_id:
                return
            if not order_id or item.state != "AVAILABLE" or item.order_id:
                raise ValueError("Visual item is unavailable or already reserved")
            item.state, item.order_id = "RESERVED", order_id

    def selectable(self, item_id, order_id):
        with self._lock:
            self._expire()
            item = self._items[item_id]
            return item.state == "AVAILABLE" or (item.state == "RESERVED" and item.order_id == order_id)

    def conflicting_labels(self, item_id):
        """Fresh labels at one physical point; never used to infer the true SKU."""
        with self._lock:
            item = self._items[item_id]
            return sorted({other.sku for other in self._items.values()
                           if other.item_id != item_id and other.sku != item.sku
                           and other.frame_id == item.frame_id
                           and other.state in ("AVAILABLE", "RESERVED", "UNKNOWN")
                           and self.clock() - other.seen_monotonic <= self.ttl
                           and dist(other.position[:2], item.position[:2]) <= self.radius
                           and abs(other.position[2] - item.position[2]) <= self.radius})

    def ambiguous_skus(self):
        with self._lock:
            return {item.sku for item in self._items.values()
                    if item.state == "UNKNOWN" and self.clock() - item.seen_monotonic <= self.ttl
                    and self.conflicting_labels(item.item_id)}

    def select_for_order(self, item_id, order):
        """Atomically exchange one untouched reservation for a visible alternative.

        Never take another order's item, exceed demand, or release carried stock.
        Alternatives are observations, not additional promises to this order.
        """
        with self._lock:
            self._expire()
            item = self._items[item_id]
            demand = {line.sku: line.quantity for line in order.lines}
            if item.sku not in demand or not self.selectable(item_id, order.order_id):
                raise ValueError("Candidate is not available for this order")
            if item_id in self._executing:
                raise ValueError("Candidate already has a physical action in flight")
            if item.order_id == order.order_id:
                return
            if any(other.item_id != item_id and other.order_id
                   and other.state in ("RESERVED", "CARRYING", "UNKNOWN")
                   and dist(other.position[:2], item.position[:2]) < self.radius
                   for other in self._items.values()):
                raise ValueError("Candidate overlaps another locked physical identity")
            owned = [other for other in self._items.values()
                     if other.sku == item.sku and other.order_id == order.order_id
                     and other.state in ("RESERVED", "CARRYING", "PLACED", "UNKNOWN")]
            if len(owned) >= demand[item.sku]:
                replaceable = next((other for other in owned if other.state == "RESERVED"
                                    and other.item_id not in self._executing), None)
                if replaceable is None:
                    raise ValueError("Order has no untouched reservation to exchange")
                replaceable.state, replaceable.order_id = "AVAILABLE", ""
            item.state, item.order_id = "RESERVED", order.order_id

    def reject_candidate(self, item_id, order_id):
        """A pre-motion safety rejection invalidates a candidate, not consumed stock."""
        with self._lock:
            item = self._items[item_id]
            if item.state not in ("AVAILABLE", "RESERVED") or item.order_id not in ("", order_id):
                raise ValueError("Cannot invalidate another order's or a carried item")
            item.state, item.order_id = "UNKNOWN", ""
            self._executing.discard(item_id)

    def reserve_order(self, order, *, excluded_item_ids=()):
        """All-or-nothing reservation. Missing observations are not proven shortages."""
        with self._lock:
            self._expire()
            excluded = set(excluded_item_ids)
            # An unsuitable destination does not make observed stock disappear.
            # Release only this order's untouched candidates, never another
            # order's reservation or an object already being carried/placed.
            for item in self._items.values():
                if item.item_id in excluded and item.order_id == order.order_id and item.state == "RESERVED" and item.item_id not in self._executing:
                    item.state, item.order_id = "AVAILABLE", ""
            selected, missing = [], {}
            blocked = [item.position for item in self._items.values()
                       if item.order_id and item.state in ("RESERVED", "CARRYING", "UNKNOWN")]
            for line in order.lines:
                owned = [item for item in self._items.values()
                         if item.order_id == order.order_id and item.sku == line.sku
                         and item.state in ("RESERVED", "CARRYING", "PLACED")]
                needed = max(0, line.quantity - len(owned))
                candidates = [item for item in self._items.values()
                              if item.sku == line.sku and item.state == "AVAILABLE" and not item.order_id
                              and item.item_id not in excluded]
                chosen = []
                for item in candidates:
                    # Cross-SKU false labels must not promise the same spatial candidate twice.
                    if any(dist(item.position[:2], point[:2]) < self.radius for point in blocked):
                        continue
                    if len(chosen) >= needed:
                        break
                    chosen.append(item)
                    blocked.append(item.position)
                if len(chosen) < needed:
                    missing[line.sku] = needed - len(chosen)
                selected.extend(chosen)
            if missing:
                return [], missing
            for item in selected:
                item.state, item.order_id = "RESERVED", order.order_id
            return [item.item_id for item in self._items.values()
                    if item.order_id == order.order_id and item.state == "RESERVED"], {}

    def release_order(self, order_id):
        """Release only untouched reservations; carried/unknown objects remain locked."""
        with self._lock:
            self._expire()
            for item in self._items.values():
                if item.order_id == order_id and item.state == "RESERVED" and item.item_id not in self._executing:
                    item.state, item.order_id = "AVAILABLE", ""
                elif item.order_id == order_id and item.state == "UNKNOWN":
                    # Caller guarantees the queued dispatch never started. Do not
                    # promote a stale observation to available on cancellation.
                    item.order_id = ""

    def summary(self):
        with self._lock:
            self._expire()
            result = {}
            for item in self._items.values():
                counts = result.setdefault(item.sku, {})
                counts[item.state] = counts.get(item.state, 0) + 1
            return result

    def invalidate_source(self):
        """Explicit source replacement: retain delivered records, discard old observations."""
        with self._lock:
            self._items = {key: item for key, item in self._items.items()
                           if item.state in ("PLACED", "COLLECTED")}
            self.epoch += 1

    def transition(self, item_id, order_id, state):
        with self._lock:
            item = self._items[item_id]
            if item.order_id != order_id:
                raise ValueError("Inventory reservation owner mismatch")
            allowed = {"RESERVED": {"CARRYING"}, "CARRYING": {"PLACED"}}
            if state not in allowed.get(item.state, set()):
                raise ValueError(f"Invalid inventory transition {item.state} -> {state}")
            item.state = state
            if state == "PLACED":
                self._executing.discard(item_id)

    def begin_execution(self, item_id, order_id):
        """Lock a validated reservation while the physical action is in flight.

        TTL still applies to untouched table observations, not an active grasp.
        CARRYING is only recorded after lift verification, never in advance.
        """
        with self._lock:
            self._expire()
            item = self._items[item_id]
            if item.state != "RESERVED" or item.order_id != order_id:
                raise ValueError("Execution requires a fresh owned reservation")
            self._executing.add(item_id)

    def has_active_execution(self):
        with self._lock:
            return bool(self._executing)

    def finish_failed(self, order_id, motion_attempted):
        with self._lock:
            for item in self._items.values():
                if item.order_id == order_id:
                    self._executing.discard(item.item_id)
                if item.order_id == order_id and item.state in ("RESERVED", "CARRYING"):
                    item.state = "UNKNOWN" if motion_attempted else "AVAILABLE"
                    if not motion_attempted:
                        item.order_id = ""

    def collect(self, order_id):
        with self._lock:
            for item in self._items.values():
                if item.order_id == order_id and item.state == "PLACED":
                    item.state = "COLLECTED"

    def _expire(self):
        for item in self._items.values():
            if item.item_id not in self._executing and item.state in ("AVAILABLE", "RESERVED") and self.clock() - item.seen_monotonic > self.ttl:
                item.state = "UNKNOWN"

    def snapshot(self):
        with self._lock:
            self._expire()
            rows = []
            for item in self._items.values():
                row = asdict(item)
                row.pop("seen_monotonic")
                row["age_sec"] = max(0.0, self.clock() - item.seen_monotonic)
                row["stale"] = row["age_sec"] > self.ttl
                rows.append(row)
            return {"epoch": self.epoch, "persistence": "in_memory", "items": rows}
