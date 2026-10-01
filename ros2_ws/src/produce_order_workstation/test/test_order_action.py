from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine as OrderLineMsg
import yaml

from produce_order_workstation.order import (
    Order,
    OrderError,
    OrderLine,
    capacity_usage,
    plan_placements,
    validate_order,
)
from produce_order_workstation.order_server import order_from_goal
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


def test_demo_scene_bins_match_order_slots():
    scene = yaml.safe_load((PACKAGE / "config/demo_scene.yaml").read_text())
    entities = {item["name"]: item for item in scene["entities"]}
    zone = CONFIG["zones"]["A"]
    assert len(zone["slots"]) == len(zone["slot_entities"]) == 4
    for name, slot in zip(zone["slot_entities"], zone["slots"]):
        assert entities[name]["prefab_name"] == "toy_box"
        translation = entities[name]["pose"]["translation"]
        assert [translation["x"], translation["y"]] == slot


def test_scene_starts_with_only_bins_and_feeds_one_product(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    workstation = Workstation(MagicMock(), CONFIG)
    scene = workstation.build_scene(PACKAGE / "config/demo_scene.yaml")
    products = [entity for entity in scene.entities if entity.prefab_name != "toy_box"]
    assert products == []
    workstation._scene_bridge = MagicMock()
    workstation._scene_prepared = True
    workstation.spawn_product("corn", 2)
    entity = workstation._scene_bridge._spawn_entity.call_args.args[0]
    assert entity.name == "order_corn_2"
    assert entity.prefab_name == "corn"
    assert (entity.pose.pose.position.x, entity.pose.pose.position.y) == tuple(
        CONFIG["stock"]["feed_position"]
    )


def test_four_items_can_share_each_bin():
    order = Order("eight-small", "A", (OrderLine("apple", 4), OrderLine("tomato", 4)))
    placements = plan_placements(order, CONFIG)
    assert [item.bin_index for item in placements] == [0, 1, 0, 1, 0, 1, 0, 1]
    assert [(item.offset_x, item.offset_y) for item in placements[::2]] == [
        (-0.03, -0.04),
        (-0.03, 0.04),
        (0.03, -0.04),
        (0.03, 0.04),
    ]


def test_six_mixed_items_have_no_arbitrary_sku_weight():
    order = Order(
        "mixed-six",
        "A",
        (
            OrderLine("corn", 2),
            OrderLine("carrot", 2),
            OrderLine("apple", 1),
            OrderLine("tomato", 1),
        ),
    )
    placements = plan_placements(order, CONFIG)
    assert capacity_usage(order, CONFIG).bins_needed == 2
    assert [item.bin_index for item in placements] == [0, 1, 0, 1, 0, 1]
    assert sorted({item.sku for item in placements}) == [
        "apple", "carrot", "corn", "tomato"
    ]

    with pytest.raises(ValueError, match="needs 5 bins"):
        validate_order(Order("too-many-bins", "A", (OrderLine("corn", 17),)), CONFIG)


def test_action_goal_uses_same_validation_as_yaml_order():
    goal = ExecuteOrder.Goal()
    goal.order_id = "action-001"
    goal.zone = "A"
    line = OrderLineMsg()
    line.sku = "corn"
    line.quantity = 1
    goal.lines = [line]
    order = order_from_goal(goal)
    validate_order(order, CONFIG)
    assert order == Order("action-001", "A", (OrderLine("corn", 1),))


def test_cancel_before_first_item_issues_no_motion():
    workstation = Workstation(MagicMock(), CONFIG, cancel_requested=lambda: True)
    workstation.move = MagicMock()
    result = workstation.run(Order("cancel-001", "A", (OrderLine("corn", 1),)))
    assert result["status"] == "CANCELLED"
    assert result["completed"] == 0
    assert result["events"][-1]["stage"] == "ORDER_CANCELLED"
    workstation.move.assert_not_called()


def test_receiving_bin_must_exist_at_configured_slot():
    workstation = Workstation(MagicMock(), CONFIG)
    bin_entity = MagicMock(name="bin_entity")
    bin_entity.name = "order_bin_A1"
    bin_entity.prefab_name = "toy_box"
    workstation._scene_bridge = MagicMock()
    workstation._scene_bridge.spawned_entities = [bin_entity]
    pose = workstation._scene_bridge.get_object_pose.return_value.pose.position
    slot = CONFIG["zones"]["A"]["slots"][0]
    pose.x, pose.y = slot
    assert workstation.slot_bin_position(CONFIG["zones"]["A"], 0, slot) == tuple(slot)

    pose.x += 0.2
    with pytest.raises(OrderError, match="not at its configured slot"):
        workstation.slot_bin_position(CONFIG["zones"]["A"], 0, slot)

    workstation._scene_bridge.spawned_entities = []
    with pytest.raises(OrderError, match="is missing"):
        workstation.slot_bin_position(CONFIG["zones"]["A"], 0, slot)


def test_scene_recovers_entities_lost_when_simulator_restarts(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    workstation = Workstation(MagicMock(), CONFIG)
    bridge = MagicMock()
    bridge.spawned_entities = [MagicMock(name="old_bin"), MagicMock(name="old_stock")]
    bridge.clear_scene.side_effect = RuntimeError(
        "Failed to delete entity old_bin. Response: "
        "Could not find entity with given name: toy_box_5"
    )
    workstation._scene_bridge = bridge
    workstation.slot_bin_position = MagicMock()

    workstation.prepare_scene(
        PACKAGE / "config/demo_scene.yaml",
        Order("retry-after-restart", "A", (OrderLine("apple", 1),)),
    )

    assert bridge.spawned_entities == []
    assert bridge.clear_scene.call_count == 2
    bridge.setup_scene.assert_called_once()


def test_scene_does_not_hide_real_cleanup_errors():
    workstation = Workstation(MagicMock(), CONFIG)
    bridge = MagicMock()
    bridge.spawned_entities = [MagicMock(name="old_bin")]
    bridge.clear_scene.side_effect = RuntimeError("delete service timed out")
    workstation._scene_bridge = bridge

    with pytest.raises(RuntimeError, match="delete service timed out"):
        workstation.prepare_scene(
            PACKAGE / "config/demo_scene.yaml",
            Order("cleanup-error", "A", (OrderLine("apple", 1),)),
        )
    assert len(bridge.spawned_entities) == 1
    bridge.setup_scene.assert_not_called()


def test_bin_release_retracts_before_verification(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    workstation = Workstation(MagicMock(), CONFIG)
    workstation._scene_bridge = MagicMock()
    workstation._scene_prepared = True
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    source = (0.5, 0.4, 0.03)
    workstation.slot_bin_position = MagicMock(return_value=slot)
    workstation.locate_source = MagicMock(return_value=(source, object()))
    workstation.observe_after_place = MagicMock(
        return_value=[(slot[0], slot[1], source[2])]
    )
    workstation.move = MagicMock()
    workstation.spawn_product = MagicMock()

    result = workstation.run(Order("bin-order", "A", (OrderLine("corn", 1),)))

    assert result["status"] == "COMPLETED"
    assert workstation.move.call_args_list == [
        call(source, grip="grab"),
        call(
            (slot[0], slot[1], source[2]),
            grip="release",
            target_z=CONFIG["motion"]["bin_release_z_m"],
        ),
        call(
            (slot[0], slot[1], source[2]),
            grip="release",
            target_z=CONFIG["motion"]["bin_clearance_z_m"],
        ),
    ]
    assert [event["stage"] for event in result["events"]].index(
        "RETRACTING"
    ) < [event["stage"] for event in result["events"]].index("VERIFYING")


def test_final_audit_catches_item_displaced_by_later_placement(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    workstation = Workstation(MagicMock(), CONFIG)
    workstation._scene_bridge = MagicMock()
    workstation._scene_prepared = True
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    workstation.slot_bin_position = MagicMock(return_value=slot)
    source = (0.32, -0.07, 0.03)
    workstation.locate_source = MagicMock(
        side_effect=[(source, object()), (source, object())]
    )
    workstation.observe_after_place = MagicMock(
        side_effect=[
            [(slot[0], slot[1], 0.05)],
            [(slot[0], slot[1], 0.05)],
            [(slot[0] + 0.20, slot[1], 0.05)],
        ]
    )
    workstation.move = MagicMock()
    workstation.spawn_product = MagicMock()

    result = workstation.run(Order("displaced", "A", (OrderLine("apple", 2),)))

    assert result["status"] == "FAILED"
    assert result["completed"] == 2
    assert "Final audit found apple outside bin A1" in result["error"]
    assert "FINAL_AUDIT" in [event["stage"] for event in result["events"]]
