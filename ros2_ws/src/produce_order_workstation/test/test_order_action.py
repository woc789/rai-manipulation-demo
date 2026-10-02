from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
import yaml

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine as OrderLineMsg
from produce_order_workstation.order import Order, OrderError, OrderLine, PerceptionMismatch, plan_placements, readable_error, validate_order
from produce_order_workstation.order_server import order_from_goal, state_after_order
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


def test_scene_has_four_bins_and_mixed_order_independent_stock():
    assert CONFIG["agent"]["model"] == "qwen3.8-flash"
    scene = yaml.safe_load((PACKAGE / "config/demo_scene.yaml").read_text())
    entities = {item["name"]: item for item in scene["entities"]}
    zone = CONFIG["zones"]["A"]
    assert zone["sku_bins"] == {"corn": 0, "carrot": 1, "apple": 2, "tomato": 3}
    for name, slot in zip(zone["slot_entities"], zone["slots"]):
        assert entities[name]["prefab_name"] == "toy_box"
        pos = entities[name]["pose"]["translation"]
        assert [pos["x"], pos["y"]] == slot
    stock = Counter(item["prefab_name"] for item in entities.values() if item["prefab_name"] != "toy_box")
    assert stock == CONFIG["stock"]["initial_counts"]
    scene_config = Workstation(MagicMock(), CONFIG).build_scene(PACKAGE / "config/demo_scene.yaml")
    assert len(scene_config.entities) == 20


def test_stock_quantity_is_distinct_from_bin_capacity():
    assert CONFIG["stock"]["initial_counts"]["apple"] == 4
    assert CONFIG["zones"]["A"]["bin_capacity_items"] == 4
    modified = deepcopy(CONFIG)
    modified["stock"]["initial_counts"]["apple"] = 3
    with pytest.raises(ValueError, match="prepared scene stock 3"):
        validate_order(Order("four-apples", "A", (OrderLine("apple", 4),)), modified)


def test_mixed_order_maps_each_sku_to_one_bin():
    order = Order("mixed", "A", (OrderLine("corn", 2), OrderLine("carrot", 2), OrderLine("apple", 1), OrderLine("tomato", 1)))
    assert [item.bin_index for item in plan_placements(order, CONFIG)] == [0, 0, 1, 1, 2, 3]


def test_action_goal_uses_shared_order_validation():
    goal = ExecuteOrder.Goal()
    goal.order_id, goal.zone = "action-001", "A"
    line = OrderLineMsg()
    line.sku, line.quantity = "corn", 1
    goal.lines = [line]
    order = order_from_goal(goal)
    validate_order(order, CONFIG)
    assert order == Order("action-001", "A", (OrderLine("corn", 1),))


def test_failed_order_without_motion_is_retryable_but_motion_requires_recovery():
    assert state_after_order("FAILED", False)[::2] == ("READY", True)
    assert state_after_order("CANCELLED", False)[::2] == ("READY", True)
    assert state_after_order("FAILED", True)[::2] == ("RECOVERY_REQUIRED", False)
    assert state_after_order("COMPLETED", True)[::2] == ("REPLENISHING", False)


def test_arrearage_is_presented_as_actionable_error():
    assert "百炼 API" in readable_error("Error code 400: Arrearage")


def test_order_start_resets_stale_visual_candidates():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._visual_candidates = {"visual_corn_1": ("corn", (0.3, 0.0, 0.05))}
    station._rejected_visual_positions = {"apple": [(0.3, 0.0, 0.05)]}
    station.begin_order(Order("enough-stock", "A", (OrderLine("corn", 2),)))
    assert station._completed_by_sku == {"corn": 0}
    assert station._visual_candidates == {}
    assert station._rejected_visual_positions == {}


def test_cancel_before_agent_issues_no_motion():
    station = Workstation(MagicMock(), CONFIG, cancel_requested=lambda: True)
    station._scene_prepared = True
    station.move = MagicMock()
    result = station.run(Order("cancel", "A", (OrderLine("corn", 1),)))
    assert result["status"] == "CANCELLED"
    station.move.assert_not_called()


def test_bin_must_exist_at_slot():
    station = Workstation(MagicMock(), CONFIG)
    entity = SimpleNamespace(name="order_bin_A1", prefab_name="toy_box")
    station._scene_bridge = MagicMock()
    station._scene_bridge.spawned_entities = [entity]
    point = station._scene_bridge.get_object_pose.return_value.pose.position
    slot = CONFIG["zones"]["A"]["slots"][0]
    point.x, point.y = slot
    assert station.slot_bin_position(CONFIG["zones"]["A"], 0, slot) == tuple(slot)
    point.x += 0.2
    with pytest.raises(OrderError, match="not at its configured slot"):
        station.slot_bin_position(CONFIG["zones"]["A"], 0, slot)


def test_visual_tool_returns_detected_coordinates_without_simulator_labels():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._completed_by_sku = {"apple": 0}
    station.detect = MagicMock(return_value=[(0.29, -0.1, 0.04), (0.40, 0.1, 0.04)])
    station._source_entities = MagicMock(side_effect=AssertionError("no oracle observation"))
    result = station.get_object_positions(
        Order("vision", "A", (OrderLine("apple", 1),)), "apple"
    )
    assert result["source"] == "rgbd_detection_segmentation_depth_tf"
    assert result["frame"] == "panda_link0"
    assert len(result["detections"]) == 2
    assert all(item["detection_id"].startswith("visual_apple_") for item in result["detections"])
    station._source_entities.assert_not_called()


def test_new_perception_invalidates_old_ids_even_for_other_skus():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.detect = MagicMock(return_value=[(0.29, -0.1, 0.04)])
    order = Order("fresh-id", "A", (OrderLine("corn", 1), OrderLine("apple", 1)))
    first = station.get_object_positions(order, "corn")["detections"][0]["detection_id"]
    second = station.get_object_positions(order, "apple")["detections"][0]["detection_id"]
    assert first != second
    assert first not in station._visual_candidates
    assert second in station._visual_candidates
    station.move = MagicMock()
    with pytest.raises(PerceptionMismatch, match="stale"):
        station.pick_and_place(order, first)
    station.move.assert_not_called()
    third = station.get_object_positions(order, "apple")["detections"][0]["detection_id"]
    assert second != third
    assert second not in station._visual_candidates


def test_agent_tool_rejects_unknown_or_unrequested_visual_id_before_motion():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.move = MagicMock()
    with pytest.raises(OrderError, match="Unknown or stale"):
        station.pick_and_place(Order("invalid", "A", (OrderLine("corn", 1),)), "invented")
    station._visual_candidates = {"visual_carrot_1": ("carrot", (0.26, -0.27, 0.03))}
    with pytest.raises(OrderError, match="not needed"):
        station.pick_and_place(Order("wrong-sku", "A", (OrderLine("corn", 1),)), "visual_carrot_1")
    station.move.assert_not_called()


def test_agent_chosen_entity_runs_motion_and_audit(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._completed_by_sku = {"corn": 0}
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    source = (0.29, -0.12, 0.04)
    entity = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    station._visual_candidates = {"visual_corn_1": ("corn", source)}
    station.detect = MagicMock(return_value=[source])
    station._source_entities = MagicMock(return_value={entity.name: (entity, source)})
    station.slot_bin_position = MagicMock(return_value=slot)
    station.observe_after_place = MagicMock(return_value=[(*slot, 0.05)])
    station.move = MagicMock()
    order = Order("motion", "A", (OrderLine("corn", 1),))
    assert station.pick_and_place(order, "visual_corn_1")["bin"] == "A1"
    assert station.motion_attempted is True
    assert station.move.call_args_list == [
        call(source, grip="grab"),
        call((*slot, source[2]), grip="release", target_z=CONFIG["motion"]["bin_release_z_m"]),
        call((*slot, source[2]), grip="release", target_z=CONFIG["motion"]["bin_clearance_z_m"]),
    ]
    assert station.finish_order(order)["status"] == "verified"
    station.observe_after_place.return_value = [(slot[0] + 0.2, slot[1], 0.05)]
    with pytest.raises(OrderError, match="Final audit"):
        station.finish_order(order)


def test_misclassified_visual_candidate_cannot_move_robot():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    point = (0.26, -0.38, 0.04)
    station._visual_candidates = {"visual_apple_1": ("apple", point)}
    station.detect = MagicMock(return_value=[point])
    tomato = SimpleNamespace(name="stock_tomato_1", prefab_name="tomato")
    station._source_entities = MagicMock(return_value={tomato.name: (tomato, point)})
    station.move = MagicMock()
    with pytest.raises(PerceptionMismatch, match="safety check"):
        station.pick_and_place(
            Order("false-label", "A", (OrderLine("apple", 1),)), "visual_apple_1"
        )
    station.move.assert_not_called()
    assert station.motion_attempted is False
    assert station._rejected_visual_positions["apple"] == [point]
    result = station.get_object_positions(
        Order("false-label", "A", (OrderLine("apple", 1),)), "apple"
    )
    assert result["detections"] == []


class FakeModel:
    def __init__(self, calls):
        self.calls = iter(calls)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **_):
        name, arguments = next(self.calls)
        function = SimpleNamespace(name=name, arguments=json.dumps(arguments))
        tool_call = SimpleNamespace(id=f"test-{name}", function=function)
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_agent_loop_uses_model_chosen_perception_and_motion_tools():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.get_object_positions = MagicMock(return_value={"detections": [{"detection_id": "visual_corn_2"}]})
    station.pick_and_place = MagicMock(return_value={"placed": "visual_corn_2"})
    station.finish_order = MagicMock(return_value={"status": "verified"})
    model = FakeModel([("get_object_positions", {"sku": "corn"}), ("pick_and_place", {"detection_id": "visual_corn_2"}), ("finish_order", {})])
    result = station.run(Order("agent", "A", (OrderLine("corn", 1),)), model=model)
    assert result["status"] == "COMPLETED"
    station.get_object_positions.assert_called_once()
    assert station.pick_and_place.call_args.args[1] == "visual_corn_2"
    assert [item["stage"] for item in result["events"]].count("AGENT_TOOL_CALL") == 3


def test_agent_rejects_pick_without_perception_and_allows_recovery():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.get_object_positions = MagicMock(return_value={"detections": [{"detection_id": "visual_corn_1"}]})
    station.pick_and_place = MagicMock(return_value={"placed": "visual_corn_1"})
    station.finish_order = MagicMock(return_value={"status": "verified"})
    model = FakeModel([
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("get_object_positions", {"sku": "corn"}),
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("finish_order", {}),
    ])
    result = station.run(Order("no-inspection", "A", (OrderLine("corn", 1),)), model=model)
    assert result["status"] == "COMPLETED"
    rejected = [event for event in result["events"] if event["stage"] == "AGENT_TOOL_RESULT" and event["result"].get("retryable")]
    assert len(rejected) == 1
    assert rejected[0]["result"]["required_tool"] == "get_object_positions"
    station.pick_and_place.assert_called_once()


def test_agent_retries_after_successful_pick_without_reperception():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.get_object_positions = MagicMock(return_value={"detections": [{"detection_id": "visual_corn_1"}]})
    station.pick_and_place = MagicMock(return_value={"placed": "visual_corn_1"})
    station.finish_order = MagicMock(return_value={"status": "verified"})
    model = FakeModel([
        ("get_object_positions", {"sku": "corn"}),
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("get_object_positions", {"sku": "corn"}),
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("finish_order", {}),
    ])
    result = station.run(Order("stale-after-pick", "A", (OrderLine("corn", 2),)), model=model)
    assert result["status"] == "COMPLETED"
    assert station.pick_and_place.call_count == 2
    assert station.get_object_positions.call_count == 2
    assert sum(event["stage"] == "AGENT_TOOL_RESULT" and event["result"].get("retryable", False) for event in result["events"]) == 1
