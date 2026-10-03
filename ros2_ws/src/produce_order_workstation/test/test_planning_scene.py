"""The simulator scene and MoveIt scene must agree before accepting orders."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from moveit_msgs.msg import CollisionObject

from produce_order_workstation import planning_scene
from produce_order_workstation import runtime


CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "config/workstation.yaml").read_text()
)
BIN_POSITIONS = {
    name: (slot[0], slot[1], 0.05)
    for zone in CONFIG["zones"].values()
    for name, slot in zip(zone["slot_entities"], zone["slots"])
}


def test_collision_geometry_has_low_table_and_open_top_bins():
    objects = planning_scene.build_collision_objects(CONFIG, BIN_POSITIONS)
    assert {obj.id for obj in objects} == {
        "produce_workstation/table",
        *(f"produce_workstation/{name}" for name in BIN_POSITIONS),
    }
    table = objects[0]
    assert len(table.primitives) == 1
    table_top = table.primitive_poses[0].position.z + table.primitives[0].dimensions[2] / 2
    assert table_top == pytest.approx(0.01)
    for obj in objects[1:]:
        assert obj.header.frame_id == "panda_link0"
        assert len(obj.primitives) == 5  # floor plus four separate walls
        floor = obj.primitive_poses[0]
        assert floor.position.z + obj.primitives[0].dimensions[2] / 2 == pytest.approx(0.04)
        assert max(
            pose.position.z + primitive.dimensions[2] / 2
            for pose, primitive in zip(obj.primitive_poses, obj.primitives)
        ) == pytest.approx(0.11)


def test_collision_geometry_rejects_missing_bin_or_invalid_size():
    positions = dict(BIN_POSITIONS)
    positions.pop("order_bin_A4")
    with pytest.raises(ValueError, match="do not match"):
        planning_scene.build_collision_objects(CONFIG, positions)
    broken = {**CONFIG, "planning_scene": {**CONFIG["planning_scene"], "bin_wall_height_m": -1}}
    with pytest.raises(ValueError, match="bin wall height"):
        planning_scene.build_collision_objects(broken, BIN_POSITIONS)


def test_geometry_check_detects_wrong_size_and_normalized_pose():
    original = planning_scene.build_collision_objects(CONFIG, BIN_POSITIONS)[0]
    normalized = planning_scene.build_collision_objects(CONFIG, BIN_POSITIONS)[0]
    original_center = normalized.primitive_poses[0].position
    normalized.pose.position.x = original_center.x
    normalized.pose.position.y = original_center.y
    normalized.pose.position.z = original_center.z
    normalized.primitive_poses[0].position.x = 0.0
    normalized.primitive_poses[0].position.y = 0.0
    normalized.primitive_poses[0].position.z = 0.0
    assert planning_scene._geometry_matches(original, normalized)
    normalized.primitives[0].dimensions[2] += 0.01
    assert not planning_scene._geometry_matches(original, normalized)


def test_sync_removes_only_own_stale_objects_and_verifies_readback(monkeypatch):
    apply_client = MagicMock()
    get_client = MagicMock()
    node = MagicMock()
    node.create_client.side_effect = [apply_client, get_client]
    sync = planning_scene.PlanningSceneSync(node)
    desired = planning_scene.build_collision_objects(CONFIG, BIN_POSITIONS)
    other = CollisionObject(id="another_package/keep")
    stale = CollisionObject(id="produce_workstation/old_bin")
    readbacks = iter([
        SimpleNamespace(scene=SimpleNamespace(world=SimpleNamespace(collision_objects=[other, stale]))),
        SimpleNamespace(scene=SimpleNamespace(world=SimpleNamespace(collision_objects=[other, *desired]))),
    ])

    def future_result(future, timeout_sec):
        assert timeout_sec == 10.0
        if future is get_client.call_async.return_value:
            return next(readbacks)
        return SimpleNamespace(success=True)

    monkeypatch.setattr(planning_scene, "get_future_result", future_result)
    assert sync.synchronize(CONFIG, BIN_POSITIONS) == [obj.id for obj in desired]
    changes = apply_client.call_async.call_args.args[0].scene.world.collision_objects
    assert changes[0].id == stale.id
    assert changes[0].operation == CollisionObject.REMOVE
    assert other.id not in {obj.id for obj in changes}
    assert {obj.id for obj in changes[1:]} == {obj.id for obj in desired}


def test_sync_fails_closed_when_moveit_does_not_confirm_objects(monkeypatch):
    node = MagicMock()
    sync = planning_scene.PlanningSceneSync(node)
    replies = iter([
        SimpleNamespace(scene=SimpleNamespace(world=SimpleNamespace(collision_objects=[]))),
        SimpleNamespace(success=True),
        SimpleNamespace(scene=SimpleNamespace(world=SimpleNamespace(collision_objects=[]))),
    ])
    monkeypatch.setattr(
        planning_scene, "get_future_result", lambda *_args, **_kwargs: next(replies)
    )
    with pytest.raises(RuntimeError, match="did not match"):
        sync.synchronize(CONFIG, BIN_POSITIONS)


def test_scene_preparation_requires_moveit_sync(monkeypatch):
    station = runtime.Workstation(MagicMock(), CONFIG)
    bridge = MagicMock()
    bridge.spawned_entities = []
    bins = [
        SimpleNamespace(name=name, prefab_name="toy_box")
        for name in BIN_POSITIONS
    ]
    bridge.setup_scene.side_effect = lambda _config: bridge.spawned_entities.extend(bins)
    bridge.get_object_pose.side_effect = lambda entity: SimpleNamespace(
        pose=SimpleNamespace(position=SimpleNamespace(
            x=BIN_POSITIONS[entity.name][0],
            y=BIN_POSITIONS[entity.name][1],
            z=BIN_POSITIONS[entity.name][2],
        ))
    )
    station._scene_bridge = bridge
    station._planning_scene = MagicMock()
    station._planning_scene.synchronize.side_effect = RuntimeError("MoveIt unavailable")
    monkeypatch.setattr(station, "build_scene", lambda _path: MagicMock())
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="MoveIt unavailable"):
        station.prepare_scene(Path("unused.yaml"))
    assert station._scene_prepared is False
    passed_positions = station._planning_scene.synchronize.call_args.args[1]
    assert passed_positions == BIN_POSITIONS
