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


class OrderCancelled(OrderError):
    """Cancellation was accepted at a safe item boundary."""


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


@dataclass(frozen=True)
class Placement:
    sku: str
    bin_index: int
    offset_y: float


def load_order(path: Path, config: dict) -> Order:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Order must be a YAML mapping")
    order_id = raw.get("order_id")
    zone = raw.get("zone")
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    lines = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each item must be a mapping")
        sku, quantity = item.get("sku"), item.get("quantity")
        lines.append(OrderLine(sku, quantity))
    order = Order(order_id, zone, tuple(lines))
    validate_order(order, config)
    return order


def validate_order(order: Order, config: dict) -> None:
    """Apply the same rules to YAML orders and ROS Action goals."""
    if not isinstance(order.order_id, str) or not re.fullmatch(
        r"[\w-]{1,64}", order.order_id
    ):
        raise ValueError("order_id must contain 1-64 letters, digits, _ or -")
    if not isinstance(order.zone, str) or order.zone not in config["zones"]:
        raise ValueError(f"Unknown destination zone: {order.zone}")
    if not order.lines:
        raise ValueError("items must be a non-empty list")
    seen = set()
    for line in order.lines:
        if not isinstance(line.sku, str) or line.sku not in config["aliases"]:
            raise ValueError(f"Unknown SKU: {line.sku}")
        if line.sku in seen:
            raise ValueError(f"Duplicate SKU: {line.sku}")
        if type(line.quantity) is not int or line.quantity < 1:
            raise ValueError(f"Quantity for {line.sku} must be a positive integer")
        seen.add(line.sku)
    zone = config["zones"][order.zone]
    slots = zone["slots"]
    capacity = zone["bin_capacity_units"]
    weights = zone["sku_space_units"]
    if any(
        line.sku not in weights or weights[line.sku] not in (1, capacity)
        for line in order.lines
    ):
        raise ValueError("SKU capacity configuration is invalid")
    required = sum(line.quantity * weights[line.sku] for line in order.lines)
    available = len(slots) * capacity
    if required > available:
        raise ValueError(
            f"Order needs {required} capacity units but zone {order.zone} has {available}"
        )
    if order.total_items > len(config["stock"]["positions"]):
        raise ValueError("Order exceeds the number of available source positions")


def plan_placements(order: Order, config: dict) -> tuple[Placement, ...]:
    """Reserve whole bins for large produce; share each other bin between two small items."""
    validate_order(order, config)
    zone = config["zones"][order.zone]
    skus = [line.sku for line in order.lines for _ in range(line.quantity)]
    weights = zone["sku_space_units"]
    capacity = zone["bin_capacity_units"]
    large_indices = [i for i, sku in enumerate(skus) if weights[sku] == capacity]
    small_indices = [i for i, sku in enumerate(skus) if weights[sku] == 1]
    assignments: list[Placement | None] = [None] * len(skus)
    for bin_index, item_index in enumerate(large_indices):
        assignments[item_index] = Placement(skus[item_index], bin_index, 0.0)
    free_bins = len(zone["slots"]) - len(large_indices)
    for index, item_index in enumerate(small_indices):
        bin_index = len(large_indices) + index % free_bins
        second = index >= free_bins
        pair_exists = index + free_bins < len(small_indices) or second
        offset_y = (0.04 if second else -0.04) if pair_exists else 0.0
        assignments[item_index] = Placement(skus[item_index], bin_index, offset_y)
    return tuple(assignments)


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
