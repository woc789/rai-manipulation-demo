"""Order rules independent of ROS and the simulator."""

from dataclasses import dataclass
from math import hypot, isfinite
from pathlib import Path
import re

import yaml


CENTROID = re.compile(
    r"Centroid\(x=([-+0-9.eE]+), y=([-+0-9.eE]+), z=([-+0-9.eE]+)\)"
)


class OrderError(RuntimeError):
    """An order cannot safely continue."""


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class Order:
    order_id: str
    zone: str
    lines: tuple[OrderLine, ...]

    @property
    def total_items(self) -> int:
        return sum(line.quantity for line in self.lines)


def load_order(path: Path, config: dict) -> Order:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Order must be a YAML mapping")
    order_id = raw.get("order_id")
    zone = raw.get("zone")
    items = raw.get("items")
    if not isinstance(order_id, str) or not re.fullmatch(r"[\w-]{1,64}", order_id):
        raise ValueError("order_id must contain 1-64 letters, digits, _ or -")
    if not isinstance(zone, str) or zone not in config["zones"]:
        raise ValueError(f"Unknown destination zone: {zone}")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    lines = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each item must be a mapping")
        sku, quantity = item.get("sku"), item.get("quantity")
        if not isinstance(sku, str) or sku not in config["aliases"]:
            raise ValueError(f"Unknown SKU: {sku}")
        if sku in seen:
            raise ValueError(f"Duplicate SKU: {sku}")
        if type(quantity) is not int or quantity < 1:
            raise ValueError(f"Quantity for {sku} must be a positive integer")
        seen.add(sku)
        lines.append(OrderLine(sku, quantity))
    order = Order(order_id, zone, tuple(lines))
    slots = config["zones"][zone]["slots"]
    if order.total_items > len(slots):
        raise ValueError(
            f"Order has {order.total_items} items but zone {zone} has {len(slots)} slots"
        )
    return order


def parse_centroids(result: str) -> list[tuple[float, float, float]]:
    """Decode the current RAI tool's human-readable centroid result."""
    points = [tuple(map(float, match)) for match in CENTROID.findall(result)]
    if any(not all(isfinite(value) for value in point) for point in points):
        raise OrderError("Perception returned a non-finite coordinate")
    if "Timeout:" in result:
        raise OrderError(result)
    return points


def distance_xy(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return hypot(first[0] - second[0], first[1] - second[1])


def choose_source(
    points: list[tuple[float, float, float]], config: dict, zone: dict
) -> tuple[float, float, float]:
    """Pick a reachable object that is not already in the destination zone."""
    x_min, x_max = config["workspace"]["x"]
    y_min, y_max = config["workspace"]["y"]
    radius = config["workspace"]["source_exclusion_radius_m"]
    candidates = [
        point
        for point in points
        if x_min <= point[0] <= x_max
        and y_min <= point[1] <= y_max
        and all(distance_xy(point, slot) > radius for slot in zone["slots"])
    ]
    if not candidates:
        raise OrderError("No reachable, unpicked object was detected")
    return min(candidates, key=lambda point: abs(point[1]))


def verify_placement(
    points: list[tuple[float, float, float]],
    source: tuple[float, float, float],
    slot: tuple[float, float],
    tolerance_m: float,
    source_exclusion_radius_m: float,
) -> bool:
    """Require an object near the destination and none near its old location."""
    at_slot = any(distance_xy(point, slot) <= tolerance_m for point in points)
    left_source = all(
        distance_xy(point, source) > source_exclusion_radius_m for point in points
    )
    return at_slot and left_source
