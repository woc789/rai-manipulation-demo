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


def test_scene_spawns_exactly_the_ordered_stock():
    workstation = Workstation(MagicMock(), CONFIG)
    order = Order(
        "four-items",
        "A",
        (OrderLine("corn", 2), OrderLine("apple", 1), OrderLine("tomato", 1)),
    )
    scene = workstation.build_scene(PACKAGE / "config/demo_scene.yaml", order)
    products = [entity for entity in scene.entities if entity.prefab_name != "toy_box"]
    assert [entity.name for entity in products] == [
        "order_corn_1",
        "order_corn_2",
        "order_apple_1",
        "order_tomato_1",
    ]
    assert [entity.prefab_name for entity in products] == [
        "corn", "corn", "apple", "tomato"
    ]
    assert [(item.pose.pose.position.x, item.pose.pose.position.y) for item in products] == [
        tuple(position) for position in CONFIG["stock"]["positions"][:4]
    ]


def test_two_small_items_can_share_each_bin():
    order = Order("eight-small", "A", (OrderLine("apple", 4), OrderLine("tomato", 4)))
    placements = plan_placements(order, CONFIG)
    assert [item.bin_index for item in placements] == [0, 1, 2, 3, 0, 1, 2, 3]
    assert [item.offset_y for item in placements] == [-0.04] * 4 + [0.04] * 4


def test_large_items_reserve_whole_bins_before_small_items():
    order = Order("mixed-six", "A", (OrderLine("apple", 4), OrderLine("corn", 2)))
    placements = plan_placements(order, CONFIG)
    assert [item.bin_index for item in placements] == [2, 3, 2, 3, 0, 1]
    assert [item.offset_y for item in placements] == [-0.04, -0.04, 0.04, 0.04, 0, 0]

    with pytest.raises(ValueError, match="capacity units"):
        validate_order(Order("too-many", "A", (OrderLine("corn", 5),)), CONFIG)


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


def test_bin_release_retracts_before_verification(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    workstation = Workstation(MagicMock(), CONFIG)
    workstation._scene_bridge = MagicMock()
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    source = (0.5, 0.4, 0.03)
    workstation.slot_bin_position = MagicMock(return_value=slot)
    workstation.locate_source = MagicMock(return_value=(source, object()))
    workstation.observe_after_place = MagicMock(
        return_value=[(slot[0], slot[1], source[2])]
    )
    workstation.move = MagicMock()

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
