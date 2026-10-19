"""Capacity admission and distinct release centres; real motion is tested separately."""

from copy import deepcopy
from itertools import combinations
from math import dist

import pytest

from db_support import CONFIG
from produce_order_workstation.order import (
    Order,
    OrderLine,
    placement_offsets,
    plan_placements,
    validate_order,
)
from produce_order_workstation.runtime import Workstation
from unittest.mock import MagicMock


@pytest.mark.parametrize("count", range(1, 9))
def test_all_supported_sizes_have_distinct_separated_centres(count):
    points = placement_offsets(count)
    assert len(points) == len(set(points)) == count
    assert all(abs(x) < 0.08 and abs(y) < 0.08 for x, y in points)
    assert all(dist(a, b) >= 0.04 - 1e-6 for a, b in combinations(points, 2))


def test_eight_mixed_items_use_one_allocated_box():
    job = Order(
        "large",
        "A",
        tuple(OrderLine(sku, 2) for sku in ("corn", "carrot", "apple", "tomato")),
    )
    result = plan_placements(job, CONFIG, bin_index=2)
    assert len(result) == 8 and {p.bin_index for p in result} == {2}
    assert len({(p.offset_x, p.offset_y) for p in result}) == 8


@pytest.mark.parametrize("count", [0, 9, True])
def test_unsupported_offset_count_is_rejected(count):
    with pytest.raises(ValueError):
        placement_offsets(count)


def test_eight_items_pass_validation_but_nine_do_not():
    validate_order(Order("eight", "A", (OrderLine("carrot", 8),)), CONFIG)
    with pytest.raises(ValueError, match="at most 8"):
        validate_order(
            Order("nine", "A", (OrderLine("carrot", 8), OrderLine("corn", 1))), CONFIG
        )
    bad = deepcopy(CONFIG)
    bad["zones"]["A"]["bin_capacity_items"] = 9
    with pytest.raises(ValueError, match="between 1 and 8"):
        validate_order(Order("bad", "A", (OrderLine("carrot", 1),)), bad)


def test_fallback_can_use_all_eight_offsets_instead_of_only_four():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    job = Order("eight", "A", (OrderLine("carrot", 8),))
    station.begin_order(job, bin_index=0)
    points = placement_offsets(8)
    # Persist the already-used targets as test fixture evidence.
    station.resources.targets = MagicMock(
        return_value=[(0.5 + x, -0.48 + y) for x, y in points[:7]]
    )
    placement, target = station._placement_target(job, "carrot", (0.3, 0.2, 0.04))
    assert placement.bin_index == 0
    assert target == pytest.approx((0.5 + points[7][0], -0.48 + points[7][1]))
