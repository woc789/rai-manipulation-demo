"""Thread-safe visual candidate ledger. Never seeded with simulator truth.

Counts describe observations, not guaranteed stock. Identity is approximate
same-SKU spatial association; all motion safety checks remain in Workstation.
"""

from dataclasses import dataclass, asdict
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
    """One-item reservations, conservative failure handling, no durable recovery."""

    def __init__(self, association_radius=0.035, ttl=60.0, *, clock=time.monotonic, wall_clock=time.time):
        if association_radius <= 0 or ttl <= 0:
            raise ValueError("Inventory radius and TTL must be positive")
        self.radius, self.ttl = association_radius, ttl
        self.clock, self.wall_clock = clock, wall_clock
        self.epoch = 0
        self._serial = 0
        self._items = {}
        self._lock = RLock()

    def reset(self):
        with self._lock:
            self._items.clear()
            self.epoch += 1

    def observe(self, sku, frame_id, points):
        if not sku or not frame_id:
            raise ValueError("Observation needs SKU and frame")
        points = [tuple(float(v) for v in point) for point in points]
        if any(len(point) != 3 or not all(isfinite(v) for v in point) for point in points):
            raise ValueError("Invalid visual observation position")
        with self._lock:
            self._expire()
            used, identities = set(), []
            for point in points:
                matches = [item for item in self._items.values()
                           if item.sku == sku and item.frame_id == frame_id and item.item_id not in used
                           and item.state in ("AVAILABLE", "UNKNOWN") and not item.order_id
                           and dist(item.position, point) <= self.radius]
                item = min(matches, key=lambda item: dist(item.position, point)) if matches else None
                if item is None:
                    self._serial += 1
                    item = InventoryItem(f"obs-{self.epoch}-{self._serial}", sku, point, frame_id,
                                         self.wall_clock(), self.clock())
                    self._items[item.item_id] = item
                item.position = point
                item.observed_unix_sec, item.seen_monotonic = self.wall_clock(), self.clock()
                item.state = "AVAILABLE"
                used.add(item.item_id)
                identities.append(item.item_id)
            # Missing detections are NOT deletions, zero stock, or confirmations.
            return identities

    def reserve(self, item_id, order_id, refreshed_position=None):
        with self._lock:
            self._expire()
            item = self._items[item_id]
            if refreshed_position is not None and item.state in ("AVAILABLE", "UNKNOWN") and not item.order_id:
                position = tuple(float(v) for v in refreshed_position)
                if len(position) != 3 or not all(isfinite(v) for v in position):
                    raise ValueError("Invalid refreshed visual position")
                if dist(item.position[:2], position[:2]) > 0.065:
                    raise ValueError("Refreshed candidate moved beyond association gate")
                item.position = position
                item.observed_unix_sec, item.seen_monotonic = self.wall_clock(), self.clock()
                item.state = "AVAILABLE"
            if not order_id or item.state != "AVAILABLE" or item.order_id:
                raise ValueError("Visual item is unavailable or already reserved")
            item.state, item.order_id = "RESERVED", order_id

    def transition(self, item_id, order_id, state):
        with self._lock:
            item = self._items[item_id]
            if item.order_id != order_id:
                raise ValueError("Inventory reservation owner mismatch")
            allowed = {"RESERVED": {"CARRYING"}, "CARRYING": {"PLACED"}}
            if state not in allowed.get(item.state, set()):
                raise ValueError(f"Invalid inventory transition {item.state} -> {state}")
            item.state = state

    def finish_failed(self, order_id, motion_attempted):
        with self._lock:
            for item in self._items.values():
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
            if item.state == "AVAILABLE" and self.clock() - item.seen_monotonic > self.ttl:
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
