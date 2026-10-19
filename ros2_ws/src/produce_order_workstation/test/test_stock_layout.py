from copy import deepcopy
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from rai_sim.simulation_bridge import SceneConfig

from produce_order_workstation.order import OrderError
from produce_order_workstation.runtime import Workstation
from produce_order_workstation.stock_layout import validate_stock_layout


PACKAGE = Path(__file__).resolve().parents[1]


def fixtures():
    config = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())
    scene = SceneConfig.load_base_config(PACKAGE / "config/demo_scene.yaml")
    return config, scene


def test_new_mixed_layout_passes_and_keeps_four_of_every_sku():
    config, scene = fixtures()
    validate_stock_layout(scene, config)
    for sku in config["aliases"]:
        assert sum(entity.prefab_name == sku for entity in scene.entities) == 4


def test_layout_keeps_both_banks_mixed_and_stronger_clearances():
    config, scene = fixtures()
    policy = config["stock"]["layout"]
    assert policy["min_center_spacing_m"] >= .10
    assert policy["min_bin_distance_m"] >= .17
    assert policy["min_base_radius_m"] >= .40
    for sign in (-1, 1):
        bank = [item for item in scene.entities if item.prefab_name in config["aliases"]
                and item.pose.pose.position.y * sign > 0]
        assert Counter(item.prefab_name for item in bank) == dict.fromkeys(config["aliases"], 2)
    validate_stock_layout(scene, config)


def test_corn_orientations_are_normalized_and_long_axis_stays_horizontal():
    _, scene = fixtures()
    for item in scene.entities:
        if item.prefab_name != "corn":
            continue
        q = item.pose.pose.orientation
        assert q.x*q.x + q.y*q.y + q.z*q.z + q.w*q.w == pytest.approx(1.0)
        # The prefab's long axis is local Z. Rotation must keep it parallel
        # to the tabletop rather than standing corn up to game detection.
        assert 1 - 2*(q.x*q.x + q.y*q.y) == pytest.approx(0.0, abs=1e-8)


def test_corn_uses_verified_foreground_patch_not_distant_side_extremes():
    _, scene = fixtures()
    for item in scene.entities:
        if item.prefab_name == "corn":
            p = item.pose.pose.position
            assert .450 <= p.x <= .560
            assert abs(p.y) <= .090


def test_corn_templates_leave_room_for_observed_settling_displacement():
    _, scene = fixtures()
    for item in scene.entities:
        if item.prefab_name != "corn":
            continue
        p = item.pose.pose.position
        # Observed roll/settling can shift X by about 12 mm. Guard both
        # workspace edges; actual settled positions still need a live audit.
        assert .215 <= p.x <= .560
        assert abs(p.y) <= .540


@pytest.mark.parametrize("position, reason", [
    ((.22, -.12), "arm base"),
    ((.57, -.23), "order-bin area"),
    ((.70, -.30), "workspace"),
    ((float("nan"), -.30), "nonfinite"),
])
def test_invalid_stock_position_fails_closed(position, reason):
    config, scene = fixtures()
    entity = next(item for item in scene.entities if item.name == "stock_tomato_2")
    entity.pose.pose.position.x, entity.pose.pose.position.y = position
    with pytest.raises(OrderError, match=reason):
        validate_stock_layout(scene, config)


def test_overlapping_stock_and_missing_inventory_are_rejected():
    config, scene = fixtures()
    stock = [item for item in scene.entities if item.prefab_name in config["aliases"]]
    stock[1].pose = deepcopy(stock[0].pose)
    with pytest.raises(OrderError, match="too close together"):
        validate_stock_layout(scene, config)
    config, scene = fixtures()
    scene.entities = [item for item in scene.entities if item.name != "stock_tomato_2"]
    with pytest.raises(OrderError, match="only 3/4 tomato"):
        validate_stock_layout(scene, config)


def test_invalid_layout_is_rejected_before_clearing_existing_scene():
    config, scene = fixtures()
    config["stock"]["layout"]["min_base_radius_m"] = 1.0
    station = Workstation(MagicMock(), config)
    station._scene_bridge = MagicMock()
    station.build_scene = MagicMock(return_value=scene)
    with pytest.raises(OrderError, match="arm base"):
        station.prepare_scene(PACKAGE / "config/demo_scene.yaml")
    station._scene_bridge.clear_scene.assert_not_called()
    station._scene_bridge.setup_scene.assert_not_called()
