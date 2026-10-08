from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from geometry_msgs.msg import Point, Pose, Quaternion

from produce_order_workstation.order import Order, OrderLine, OrderError
from produce_order_workstation.planning_scene import build_collision_objects
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())
EXPECTED = CONFIG["planning_scene"]["barrier"]["expected_pose"]
CRATE = Pose(position=Point(x=EXPECTED["position"][0], y=EXPECTED["position"][1], z=EXPECTED["position"][2]),
             orientation=Quaternion(x=EXPECTED["orientation"][0], y=EXPECTED["orientation"][1],
                                    z=EXPECTED["orientation"][2], w=EXPECTED["orientation"][3]))


def test_startup_planning_scene_has_no_receiving_bin_proxies():
    objects = build_collision_objects(CONFIG, {}, [CRATE])
    assert [o.id for o in objects] == ["produce_workstation/table", "produce_workstation/route_barrier"]
    assert len(objects[-1].primitives) == 5


def test_planning_scene_only_includes_present_order_bins():
    objects = build_collision_objects(CONFIG, {"order_bin_A2": (.57, -.23, .05)}, [CRATE])
    assert {o.id for o in objects} == {"produce_workstation/table", "produce_workstation/route_barrier", "produce_workstation/order_bin_A2"}
    with pytest.raises(ValueError, match="do not match"):
        build_collision_objects(CONFIG, {"unknown_box": (.57, -.23, .05)}, [CRATE])


def test_crate_proxy_matches_rotated_physical_collider_parts():
    obj = build_collision_objects(CONFIG, {}, [CRATE])[-1]
    floor = obj.primitives[0]
    assert floor.dimensions == pytest.approx([.03, .21, .21])
    assert obj.primitive_poses[0].position.x == pytest.approx(.43)
    # A side wall is transformed into the upper horizontal wall of a side-laid crate.
    assert sorted(p.position.z for p in obj.primitive_poses) == pytest.approx([.02, .11, .11, .11, .20])


def test_return_crate_pose_changes_fail_closed():
    shifted = deepcopy(CRATE)
    shifted.position.x += .1
    with pytest.raises(ValueError, match="moved"):
        build_collision_objects(CONFIG, {}, [shifted])
    shifted = deepcopy(CRATE)
    shifted.orientation = Quaternion(w=1.)
    with pytest.raises(ValueError, match="tipped"):
        build_collision_objects(CONFIG, {}, [shifted])


def test_crate_geometry_is_sourced_from_existing_prefab():
    import json
    prefab = json.loads((PACKAGE.parents[2] / "Project/Prefabs/toy_box.prefab").read_text())
    parts = []
    for entity in prefab["Entities"].values():
        for component in entity["Components"].values():
            if component.get("$type") == "EditorColliderComponent":
                parts.append((component["ColliderConfiguration"].get("Position", [0.,0.,0.]),
                              component["ShapeConfiguration"]["Box"]["Configuration"]))
    configured = CONFIG["planning_scene"]["barrier"]["collider_parts"]
    assert len(parts) == len(configured) == 5
    for center, size in parts:
        assert any(center == pytest.approx(part["center"]) and size == pytest.approx(part["size"]) for part in configured)


def test_new_order_spawns_exactly_its_allocated_bin_and_syncs(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    station = Workstation(MagicMock(), CONFIG)
    station._scene_bridge = MagicMock()
    station._scene_bridge.spawned_entities = []
    station._bin_templates = {"order_bin_A3": SimpleNamespace(name="order_bin_A3")}
    station.synchronize_scene = MagicMock(return_value=["produce_workstation/order_bin_A3"])
    order = Order("new", "A", (OrderLine("apple", 1),))
    station.ensure_order_bins(order, (2,))
    station._scene_bridge._spawn_entity.assert_called_once_with(station._bin_templates["order_bin_A3"])
    station.synchronize_scene.assert_called_once()
    assert station.events[-1]["stage"] == "ORDER_BIN_ARRIVED"


def test_existing_bin_cannot_be_reused_as_empty():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_bridge = MagicMock()
    station._scene_bridge.spawned_entities = [SimpleNamespace(name="order_bin_A1")]
    with pytest.raises(OrderError, match="already contains"):
        station.ensure_order_bins(Order("new", "A", (OrderLine("apple", 1),)), (0,))
    station._scene_bridge._spawn_entity.assert_not_called()


def test_remove_box_only_affects_selected_order_and_syncs():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_bridge = MagicMock()
    own = SimpleNamespace(name="order_bin_A1")
    other = SimpleNamespace(name="order_bin_A2")
    station._scene_bridge.spawned_entities = [own, other]
    station.synchronize_scene = MagicMock()
    station.remove_order_bins(Order("old", "A", (OrderLine("apple", 1),)), (0,))
    station._scene_bridge._despawn_entity.assert_called_once_with(own)
    station.synchronize_scene.assert_called_once()


def test_actual_startup_spawns_stock_and_obstacle_but_no_order_boxes(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    station = Workstation(MagicMock(), CONFIG)
    bridge = MagicMock()
    bridge.spawned_entities = []
    bridge.setup_scene.side_effect = lambda scene: bridge.spawned_entities.extend(scene.entities)
    bridge._spawn_entity.side_effect = lambda entity: bridge.spawned_entities.append(entity)
    bridge.get_object_pose.side_effect = lambda entity: SimpleNamespace(pose=entity.pose.pose)
    station._scene_bridge = bridge
    station._planning_scene = MagicMock()
    station._planning_scene.synchronize.return_value = ["produce_workstation/table", "produce_workstation/route_barrier"]
    station.prepare_scene(PACKAGE / "config/demo_scene.yaml")
    assert station._scene_prepared
    assert len(bridge.spawned_entities) == 17
    assert not any(e.name.startswith("order_bin_") for e in bridge.spawned_entities)
    assert len(station._bin_templates) == 4
    assert station._planning_scene.synchronize.call_args.args[1] == {}
