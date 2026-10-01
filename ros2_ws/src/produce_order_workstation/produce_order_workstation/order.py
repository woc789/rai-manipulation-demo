"""Order rules independent of ROS and the simulator."""

from dataclasses import dataclass
from math import ceil, hypot, isfinite
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
    offset_x: float
    offset_y: float


@dataclass(frozen=True)
class CapacityUsage:
    bins_needed: int
    bins_available: int
    items_needed: int
    items_per_bin: int


def capacity_usage(order: Order, config: dict) -> CapacityUsage:
    """Count pieces, not arbitrary SKU-specific space weights."""
    zone = config["zones"][order.zone]
    capacity = zone["bin_capacity_items"]
    bins = len(zone["slots"])
    return CapacityUsage(
        bins_needed=ceil(order.total_items / capacity),
        bins_available=bins,
        items_needed=order.total_items,
        items_per_bin=capacity,
    )


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
    capacity = zone["bin_capacity_items"]
    if type(capacity) is not int or capacity < 1 or capacity > 4:
        raise ValueError("Bin capacity must be between 1 and 4 items")
    usage = capacity_usage(order, config)
    if usage.bins_needed > usage.bins_available:
        raise ValueError(
            f"Order needs {usage.bins_needed} bins but zone {order.zone} "
            f"has {usage.bins_available} ({usage.items_per_bin} items per bin)"
        )


def plan_placements(order: Order, config: dict) -> tuple[Placement, ...]:
    """Balance the order across the fewest bins, using up to four drop points."""
    validate_order(order, config)
    skus = [line.sku for line in order.lines for _ in range(line.quantity)]
    bins_needed = capacity_usage(order, config).bins_needed
    bin_items = [[] for _ in range(bins_needed)]
    for item_index in range(len(skus)):
        bin_items[item_index % bins_needed].append(item_index)
    offsets = {
        1: [(0.0, 0.0)],
        2: [(0.0, -0.04), (0.0, 0.04)],
        3: [(-0.03, -0.04), (-0.03, 0.04), (0.03, 0.0)],
        4: [(-0.03, -0.04), (-0.03, 0.04), (0.03, -0.04), (0.03, 0.04)],
    }
    assignments: list[Placement | None] = [None] * len(skus)
    for bin_index, item_indices in enumerate(bin_items):
        for item_index, (offset_x, offset_y) in zip(
            item_indices, offsets[len(item_indices)]
        ):
            assignments[item_index] = Placement(
                skus[item_index], bin_index, offset_x, offset_y
            )
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


def verify_bin_placement(
    points: list[tuple[float, float, float]],
    source: tuple[float, float, float],
    bin_center: tuple[float, float],
    half_extents: tuple[float, float],
    source_exclusion_radius_m: float,
) -> bool:
    """Accept an item inside the bin footprint, including its corners."""
    inside = any(
        abs(point[0] - bin_center[0]) <= half_extents[0]
        and abs(point[1] - bin_center[1]) <= half_extents[1]
        for point in points
    )
    left_source = all(
        distance_xy(point, source) > source_exclusion_radius_m
        for point in points
    )
    return inside and left_source
