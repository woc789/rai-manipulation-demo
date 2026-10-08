"""Startup layout checks, not perception or a robot reachability solver.

Spawn templates are only used to validate the simulation workcell setup. These
coordinates are never returned as detections or sent to the Agent.
"""

from itertools import combinations
from math import hypot, isfinite

from .order import OrderError


def validate_stock_layout(scene, config: dict) -> None:
    policy = config["stock"].get("layout")
    if policy is None:
        return
    limits = {}
    for name in ("min_base_radius_m", "min_center_spacing_m", "min_bin_distance_m"):
        value = float(policy[name])
        if not isfinite(value) or value < 0:
            raise OrderError(f"Invalid stock layout limit: {name}")
        limits[name] = value
    stock = []
    counts = {sku: 0 for sku in config["aliases"]}
    for entity in scene.entities:
        if entity.prefab_name not in counts:
            continue
        position = entity.pose.pose.position
        x, y = position.x, position.y
        if not all(isfinite(value) for value in (x, y, position.z)):
            raise OrderError(f"Stock {entity.name} has a nonfinite position")
        if not (config["workspace"]["x"][0] <= x <= config["workspace"]["x"][1]
                and config["workspace"]["y"][0] <= y <= config["workspace"]["y"][1]):
            raise OrderError(f"Stock {entity.name} is outside the configured workspace")
        if hypot(x, y) < limits["min_base_radius_m"]:
            raise OrderError(f"Stock {entity.name} is too close to the arm base")
        for zone in config["zones"].values():
            for bx, by in zone["slots"]:
                if hypot(x - bx, y - by) < limits["min_bin_distance_m"]:
                    raise OrderError(f"Stock {entity.name} intrudes into an order-bin area")
        stock.append((entity.name, x, y))
        counts[entity.prefab_name] += 1
    for (a, ax, ay), (b, bx, by) in combinations(stock, 2):
        if hypot(ax - bx, ay - by) < limits["min_center_spacing_m"]:
            raise OrderError(f"Stock templates {a} and {b} are too close together")
    for sku, expected in config["stock"]["initial_counts"].items():
        if counts[sku] < expected:
            raise OrderError(f"Stock layout has only {counts[sku]}/{expected} {sku}")
