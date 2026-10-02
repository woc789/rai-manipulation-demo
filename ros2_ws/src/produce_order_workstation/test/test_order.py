from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from produce_order_workstation.order import (
    OrderError,
    calibrated_grasp_point,
    choose_source,
    load_order,
    parse_centroids,
    verify_bin_placement,
    verify_placement,
)
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


def test_carrot_visual_grasp_calibration_is_sku_specific():
    visual = (0.414, -0.347, 0.041)
    assert calibrated_grasp_point(visual, "carrot", CONFIG) == pytest.approx(
        (0.390, -0.347, 0.027)
    )
    assert calibrated_grasp_point(visual, "corn", CONFIG) == visual


def test_demo_order_has_two_unique_items_and_slots():
    order = load_order(PACKAGE / "config/demo_order.yaml", CONFIG)
    assert order.total_items == 2
    assert [line.sku for line in order.lines] == ["corn", "carrot"]


def test_duplicate_sku_is_rejected(tmp_path):
    order_path = tmp_path / "duplicate.yaml"
    order_path.write_text(
        "order_id: duplicate\nzone: A\nitems:\n"
        "  - {sku: corn, quantity: 1}\n"
        "  - {sku: corn, quantity: 1}\n"
    )
    with pytest.raises(ValueError, match="Duplicate SKU"):
        load_order(order_path, CONFIG)


def test_perception_result_is_parsed_and_destination_object_excluded():
    slot_x, slot_y = CONFIG["zones"]["A"]["slots"][0]
    source_x, source_y = 0.29, -0.12
    result = (
        "Centroids of detected corn cob in panda_link0 frame: "
        f"[Centroid(x={source_x:.4f}, y={source_y:.4f}, z=0.0500), "
        f"Centroid(x={slot_x:.4f}, y={slot_y:.4f}, z=0.0500)]."
    )
    points = parse_centroids(result)
    assert choose_source(points, CONFIG, CONFIG["zones"]["A"]) == (
        source_x, source_y, 0.05
    )


def test_placement_requires_new_slot_occupancy_and_source_departure():
    source = (0.5, 0.4, 0.05)
    slot = CONFIG["zones"]["A"]["slots"][0]
    placed = (slot[0] + 0.005, slot[1] + 0.005, 0.05)
    assert verify_placement([placed], source, slot, 0.07, 0.08)
    assert not verify_placement([source], source, slot, 0.07, 0.08)
    assert not verify_placement(
        [source, placed], source, slot, 0.07, 0.08
    )


def test_bin_footprint_accepts_corners_but_rejects_outside_items():
    center = (0.50, -0.48)
    source = (0.32, -0.07, 0.03)
    corner_inside = (0.558, -0.526, 0.05)
    assert verify_bin_placement([corner_inside], source, center, (0.08, 0.08), 0.08)
    assert not verify_bin_placement(
        [(0.59, -0.526, 0.05)], source, center, (0.08, 0.08), 0.08
    )
    assert not verify_bin_placement([source], source, center, (0.08, 0.08), 0.08)


def test_no_reachable_object_stops_before_motion():
    with pytest.raises(OrderError, match="No reachable"):
        choose_source([(0.7, 0.7, 0.05)], CONFIG, CONFIG["zones"]["A"])


def test_simulation_entity_lookup_is_only_a_hidden_safety_oracle():
    corn = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    carrot = SimpleNamespace(name="stock_carrot_1", prefab_name="carrot")
    positions = {
        id(corn): SimpleNamespace(x=0.5, y=0.36, z=0.04),
        id(carrot): SimpleNamespace(x=0.5, y=-0.3, z=0.04),
    }
    workstation = Workstation(MagicMock(), CONFIG)
    workstation._scene_bridge = SimpleNamespace(
        spawned_entities=[carrot, corn],
        get_object_pose=lambda entity: SimpleNamespace(
            pose=SimpleNamespace(position=positions[id(entity)])
        ),
    )
    workstation._scene_prepared = True
    workstation.detect = MagicMock(side_effect=AssertionError("oracle must not run vision"))

    truth = workstation._source_entities(CONFIG["zones"]["A"])
    assert truth[corn.name][0] is corn
    assert truth[corn.name][1] == (0.5, 0.36, 0.04)
    assert workstation.observe_after_place("corn", corn) == [truth[corn.name][1]]
    workstation.detect.assert_not_called()
