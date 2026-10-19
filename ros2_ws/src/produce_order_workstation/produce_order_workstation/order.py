"""Order rules independent of ROS and the simulator."""

from dataclasses import dataclass
from math import hypot, isfinite
from pathlib import Path
import re

import yaml


CENTROID = re.compile(
    r"Centroid\(x=([-+0-9.eE]+), y=([-+0-9.eE]+), z=([-+0-9.eE]+)\)"
)
MAX_BIN_ITEMS = 8


class OrderError(RuntimeError):
    """An order cannot safely continue."""


class MotionPlanRejected(OrderError):
    """Server confirms no grasp transit/contact/closing was executed."""


class OrderCancelled(OrderError):
    """Cancellation was accepted at a safe item boundary."""


class OrderPaused(OrderError):
    """A verified item has been placed and the arm has retreated; yield to an urgent job."""


class PerceptionMismatch(OrderError):
    """A visual candidate failed the simulator safety interlock before motion."""

    def __init__(self, message: str, *, code="TARGET_UNCERTAIN", reobserve=True):
        super().__init__(message)
        self.code = code
        self.reobserve = reobserve


def readable_error(error: str) -> str:
    """Translate a known API failure without hiding the report's original error."""
    if "Arrearage" in error or "overdue-payment" in error:
        return "百炼 API 账户欠费或额度不可用；请检查百炼账户。是否需要重置场景请看工作台状态。"
    empty = re.fullmatch(
        r"No (\w+) detected after (\d+) consecutive visual attempts; (\d+) still needed",
        error,
    )
    if empty:
        sku, attempts, remaining = empty.groups()
        name = {"corn": "玉米", "carrot": "胡萝卜", "apple": "苹果", "tomato": "番茄"}.get(sku, sku)
        return f"连续 {attempts} 次视觉定位未找到{name}，还差 {remaining} 件；订单已停止。"
    filtered = re.fullmatch(
        r"No selectable (\w+) candidate after (\d+) consecutive visual attempts; localized candidates were filtered by safety or reservation rules",
        error,
    )
    if filtered:
        sku, attempts = filtered.groups()
        name = {"corn": "玉米", "carrot": "胡萝卜", "apple": "苹果", "tomato": "番茄"}.get(sku, sku)
        return f"已定位到{name}，但连续 {attempts} 次没有满足安全或预留规则的候选；请查看感知过滤记录，勿直接判定缺货。"
    return error


def calibrated_grasp_point(
    visual_point: tuple[float, float, float], sku: str, config: dict
) -> tuple[float, float, float]:
    """Apply a measured workcell calibration to a camera-derived point."""
    correction = config["motion"].get("grasp_correction_m", {}).get(sku, [0.0, 0.0, 0.0])
    if len(correction) != 3:
        raise ValueError(f"grasp_correction_m[{sku}] must contain x, y and z offsets")
    return tuple(value + offset for value, offset in zip(visual_point, correction))


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


def fulfillment_mode(config: dict) -> str:
    mode = config.get("operation_mode", "classification")
    if mode not in ("fulfillment", "classification"):
        raise ValueError("operation_mode must be fulfillment or classification")
    return mode


def capacity_usage(order: Order, config: dict) -> CapacityUsage:
    """Preview one order bin or the legacy dedicated SKU bins."""
    zone = config["zones"][order.zone]
    capacity = zone["bin_capacity_items"]
    bins = len(zone["slots"])
    return CapacityUsage(
        bins_needed=1 if order.lines and fulfillment_mode(config) == "fulfillment" else len(order.lines),
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
    if type(capacity) is not int or not 1 <= capacity <= MAX_BIN_ITEMS:
        raise ValueError(f"Bin capacity must be between 1 and {MAX_BIN_ITEMS} items")
    sku_bins = zone["sku_bins"]
    if fulfillment_mode(config) == "classification" and (set(sku_bins) != set(config["aliases"]) or set(sku_bins.values()) != set(
        range(len(zone["slots"]))
    )):
        raise ValueError("Each SKU must map to exactly one distinct receiving bin")
    if fulfillment_mode(config) == "fulfillment" and order.total_items > capacity:
        raise ValueError(f"One order bin holds at most {capacity} items; selected {order.total_items}")
    for line in order.lines:
        if line.quantity > capacity:
            raise ValueError(
                f"Quantity for {line.sku} exceeds bin "
                f"{order.zone}{sku_bins[line.sku] + 1} capacity {capacity}"
            )
        stock = config.get("stock", {}).get("initial_counts", {})
        if fulfillment_mode(config) == "classification" and line.quantity > stock.get(line.sku, 0):
            raise ValueError(
                f"Quantity for {line.sku} exceeds prepared scene stock "
                f"{stock.get(line.sku, 0)}"
            )


def placement_offsets(count: int) -> tuple[tuple[float, float], ...]:
    """Distinct release centres, not a guarantee of non-overlapping 3D packing."""
    if type(count) is not int or not 1 <= count <= MAX_BIN_ITEMS:
        raise ValueError(f"Placement count must be between 1 and {MAX_BIN_ITEMS}")
    offsets = {
        1: [(0.0, 0.0)],
        2: [(0.0, -0.04), (0.0, 0.04)],
        3: [(-0.03, -0.04), (-0.03, 0.04), (0.03, 0.0)],
        4: [(-0.03, -0.04), (-0.03, 0.04), (0.03, -0.04), (0.03, 0.04)],
    }
    if count <= 4:
        return tuple(offsets[count])
    perimeter = (
        (-0.045, -0.045), (0.045, 0.045),
        (-0.045, 0.045), (0.045, -0.045),
        (0.0, -0.045), (0.0, 0.045),
        (-0.045, 0.0), (0.045, 0.0),
    )
    return perimeter[:count]


def plan_placements(order: Order, config: dict, *, bin_index: int | None = None) -> tuple[Placement, ...]:
    """Use globally distinct offsets for one order, or per-SKU legacy offsets."""
    validate_order(order, config)
    sku_bins = config["zones"][order.zone]["sku_bins"]
    if fulfillment_mode(config) == "fulfillment":
        # None is a planning preview / standalone CLI default, not server allocation.
        selected = 0 if bin_index is None else bin_index
        if type(selected) is not int or selected not in range(len(config["zones"][order.zone]["slots"])):
            raise ValueError("Invalid allocated bin index")
        skus = [line.sku for line in order.lines for _ in range(line.quantity)]
        return tuple(Placement(sku, selected, x, y)
                     for sku, (x, y) in zip(skus, placement_offsets(order.total_items)))
    return tuple(
        Placement(line.sku, sku_bins[line.sku], offset_x, offset_y)
        for line in order.lines
        for offset_x, offset_y in placement_offsets(line.quantity)
    )


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
