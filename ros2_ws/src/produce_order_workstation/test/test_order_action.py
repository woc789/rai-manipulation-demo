from collections import Counter
from copy import deepcopy
import json
from math import hypot
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
import yaml
from action_msgs.msg import GoalStatus

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine as OrderLineMsg
from produce_order_workstation.order import Order, OrderCancelled, OrderError, OrderLine, PerceptionMismatch, plan_placements, readable_error, validate_order
from produce_order_workstation.order_server import OrderActionServer, order_from_goal, state_after_order
from produce_order_workstation import runtime
from produce_order_workstation.runtime import Workstation
from produce_order_workstation.inventory import InventoryLedger


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())
# Keep the original classification-mode regressions, alongside fulfillment tests.
CONFIG["operation_mode"] = "classification"


def test_motion_feedback_retains_order_counts_between_item_events():
    server = SimpleNamespace(workstation=SimpleNamespace(_completed_by_sku={"corn": 1, "carrot": 1}))
    handle = MagicMock()
    handle.request.lines = [OrderLineMsg(sku=sku, quantity=1) for sku in ("corn", "carrot", "apple", "tomato")]
    OrderActionServer._publish_feedback(server, handle, {"stage": "MOTION_STAGE"})
    feedback = handle.publish_feedback.call_args.args[0]
    assert (feedback.completed, feedback.total) == (2, 4)


def test_inventory_publisher_uses_typed_observations_and_reservation_owner():
    book = InventoryLedger()
    item = book.observe("corn", "panda_link0", [(0.3, -0.1, 0.04)])[0]
    book.reserve(item, "owner")
    server = SimpleNamespace(workstation=SimpleNamespace(inventory=book, _scene_prepared=True),
        config=CONFIG, _state="RUNNING", _inventory_publisher=MagicMock(), get_clock=MagicMock())
    from builtin_interfaces.msg import Time
    server.get_clock.return_value.now.return_value.to_msg.return_value = Time(sec=123)
    OrderActionServer.publish_inventory(server)
    message = server._inventory_publisher.publish.call_args.args[0]
    assert message.scene_ready and message.persistence == "in_memory"
    assert message.header.stamp.sec == 123
    assert len(message.items) == 1
    row = message.items[0]
    assert (row.item_id, row.state, row.order_id) == (item, "RESERVED", "owner")
    assert row.position.x == .3 and not row.classification_verified


def test_move_uses_motion_action_and_forwards_stage_feedback(monkeypatch):
    station = Workstation(MagicMock(), CONFIG)
    station._active_order_id = "motion-test"
    client = MagicMock()
    station._motion_client = client
    handle = MagicMock(accepted=True)
    handle.get_result_async.return_value.done.return_value = True
    handle.get_result_async.return_value.result.return_value = SimpleNamespace(
        status=GoalStatus.STATUS_SUCCEEDED,
        result=SimpleNamespace(success=True, error=""),
    )
    monkeypatch.setattr(runtime, "get_future_result", lambda *_args, **_kwargs: handle)
    station.move((0.3, 0.1, 0.05), step="grab")
    goal = client.send_goal_async.call_args.args[0]
    assert goal.step == "grab"
    assert goal.target_pose.header.frame_id == "panda_link0"
    assert goal.initial_gripper_state is True
    assert goal.final_gripper_state is False
    feedback = client.send_goal_async.call_args.kwargs["feedback_callback"]
    feedback(SimpleNamespace(feedback=SimpleNamespace(stage="grab:MOVING_TO_TARGET")))
    assert station.events[-1]["motion_stage"] == "grab:MOVING_TO_TARGET"


@pytest.mark.parametrize("step,initial,final,height", [
    ("grab", True, False, 0.15),
    ("lift", False, False, CONFIG["motion"]["post_grasp_lift_z_m"]),
    ("place", False, True, CONFIG["motion"]["bin_release_z_m"]),
    ("retreat", True, True, CONFIG["motion"]["bin_clearance_z_m"]),
])
def test_motion_phases_have_explicit_gripper_states_and_heights(monkeypatch, step, initial, final, height):
    station = Workstation(MagicMock(), CONFIG)
    client = MagicMock()
    station._motion_client = client
    handle = MagicMock(accepted=True)
    handle.get_result_async.return_value.done.return_value = True
    handle.get_result_async.return_value.result.return_value = SimpleNamespace(
        status=GoalStatus.STATUS_SUCCEEDED,
        result=SimpleNamespace(success=True, error=""),
    )
    monkeypatch.setattr(runtime, "get_future_result", lambda *_args, **_kwargs: handle)
    station.move((0.3, 0.1, 0.05), step=step)
    goal = client.send_goal_async.call_args.args[0]
    assert goal.step == step
    assert goal.initial_gripper_state is initial
    assert goal.final_gripper_state is final
    assert goal.target_pose.pose.position.z == pytest.approx(height)


@pytest.mark.parametrize("step", ["hold", "release", "unknown"])
def test_old_or_unknown_motion_steps_are_rejected_before_sending(step):
    station = Workstation(MagicMock(), CONFIG)
    station._motion_client = MagicMock()
    with pytest.raises(ValueError, match="Unsupported motion step"):
        station.move((0.3, 0.1, 0.05), step=step)
    station._motion_client.send_goal_async.assert_not_called()


def test_move_reports_motion_action_cancel_and_failure(monkeypatch):
    station = Workstation(MagicMock(), CONFIG)
    client = MagicMock()
    station._motion_client = client
    handle = MagicMock(accepted=True)
    handle.get_result_async.return_value.done.return_value = True
    monkeypatch.setattr(runtime, "get_future_result", lambda *_args, **_kwargs: handle)
    handle.get_result_async.return_value.result.return_value = SimpleNamespace(
        status=GoalStatus.STATUS_CANCELED,
        result=SimpleNamespace(success=False, error="canceled"),
    )
    with pytest.raises(OrderCancelled, match="canceled"):
        station.move((0.3, 0.1, 0.05), step="lift")
    handle.get_result_async.return_value.result.return_value = SimpleNamespace(
        status=GoalStatus.STATUS_ABORTED,
        result=SimpleNamespace(success=False, error="Initial gripper command failed"),
    )
    with pytest.raises(OrderError, match="Initial gripper command failed"):
        station.move((0.3, 0.1, 0.05), step="place")


def test_move_forwards_order_cancel_to_motion_action(monkeypatch):
    station = Workstation(MagicMock(), CONFIG, cancel_requested=lambda: True)
    client = MagicMock()
    station._motion_client = client
    handle = MagicMock(accepted=True)
    future = handle.get_result_async.return_value
    future.done.side_effect = [False, True]
    future.result.return_value = SimpleNamespace(
        status=GoalStatus.STATUS_CANCELED,
        result=SimpleNamespace(success=False, error="canceled at boundary"),
    )
    monkeypatch.setattr(runtime, "get_future_result", lambda *_args, **_kwargs: handle)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    with pytest.raises(OrderCancelled):
        station.move((0.3, 0.1, 0.05), step="lift")
    handle.cancel_goal_async.assert_called_once()


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
    barrier_names = CONFIG["planning_scene"]["barrier"]["entities"]
    assert len(barrier_names) == 1
    assert all(entities[name]["prefab_name"] == "toy_box" for name in barrier_names)
    for name, center in zip(barrier_names, [CONFIG["planning_scene"]["barrier"]["expected_pose"]["position"]]):
        pos = entities[name]["pose"]["translation"]
        assert [pos["x"], pos["y"]] == center[:2]
    stock = Counter(item["prefab_name"] for item in entities.values() if item["prefab_name"] in CONFIG["aliases"])
    assert stock == CONFIG["stock"]["initial_counts"]
    positions = [
        item["pose"]["translation"] for item in entities.values()
        if item["prefab_name"] in CONFIG["aliases"]
    ]
    assert min(
        hypot(a["x"] - b["x"], a["y"] - b["y"])
        for i, a in enumerate(positions) for b in positions[i + 1:]
    ) > 0.08
    scene_config = Workstation(MagicMock(), CONFIG).build_scene(PACKAGE / "config/demo_scene.yaml")
    assert len(scene_config.entities) == 21  # four unspawned bin templates, stock and crate


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
    assert state_after_order("COMPLETED", True)[::2] == ("READY", True)


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
    station._planning_scene = MagicMock()
    station.slot_bin_position = MagicMock(return_value=slot)
    station.observe_after_place = MagicMock(return_value=[(*slot, 0.05)])
    station.entity_position = MagicMock(return_value=(source[0], source[1], source[2] + 0.1))
    station.move = MagicMock()
    order = Order("motion", "A", (OrderLine("corn", 1),))
    assert station.pick_and_place(order, "visual_corn_1")["bin"] == "A1"
    assert station.motion_attempted is True
    assert station.move.call_args_list == [
        call(source, step="grab"),
        call(source, step="lift", target_z=CONFIG["motion"]["post_grasp_lift_z_m"]),
        call((*slot, source[2]), step="place", target_z=CONFIG["motion"]["bin_release_z_m"]),
        call((*slot, source[2]), step="retreat", target_z=CONFIG["motion"]["bin_clearance_z_m"]),
    ]
    assert any(event["stage"] == "GRASP_CHECK" and event["verified"] for event in station.events)
    carried_id = "produce_workstation/carried_stock_corn_1"
    station._planning_scene.attach_carried.assert_called_once_with(
        carried_id,
        (source[0], source[1], source[2] + 0.1),
        CONFIG["planning_scene"]["carried_radius_m"]["corn"],
        station.connector,
        CONFIG["camera"]["target_frame"],
    )
    station._planning_scene.detach_carried.assert_called_once_with(carried_id)
    assert station.finish_order(order)["status"] == "verified"
    station.observe_after_place.return_value = [(slot[0] + 0.2, slot[1], 0.05)]
    with pytest.raises(OrderError, match="Final audit"):
        station.finish_order(order)


def test_failed_lift_stops_before_transport(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._completed_by_sku = {"carrot": 0}
    visual = (0.414, -0.347, 0.041)
    actual = (0.390, -0.347, 0.027)
    entity = SimpleNamespace(name="stock_carrot_3", prefab_name="carrot")
    station._visual_candidates = {"visual_carrot_1_1": ("carrot", visual)}
    station.detect = MagicMock(return_value=[visual])
    station._source_entities = MagicMock(return_value={entity.name: (entity, actual)})
    station.slot_bin_position = MagicMock(return_value=tuple(CONFIG["zones"]["A"]["slots"][1]))
    station.entity_position = MagicMock(return_value=actual)
    station.move = MagicMock()
    order = Order("missed-grasp", "A", (OrderLine("carrot", 1),))
    with pytest.raises(OrderError, match="not lifted"):
        station.pick_and_place(order, "visual_carrot_1_1")
    corrected = pytest.approx((0.390, -0.347, 0.027))
    assert station.move.call_count == 2
    assert station.move.call_args_list[0].args[0] == corrected
    assert station.move.call_args_list[1].kwargs == {
        "step": "lift", "target_z": CONFIG["motion"]["post_grasp_lift_z_m"]
    }
    assert not any(event["stage"] == "PLACING" for event in station.events)
    assert any(event["stage"] == "GRASP_CHECK" and not event["verified"] for event in station.events)


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
        step = next(self.calls)
        selected = step if isinstance(step, list) else [step]
        tool_calls = [
            SimpleNamespace(
                id=f"test-{index}-{name}",
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
            ) for index, (name, arguments) in enumerate(selected)
        ]
        message = SimpleNamespace(content=None, tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_agent_loop_uses_model_chosen_perception_and_motion_tools():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.get_object_positions = MagicMock(return_value={"detections": [{"detection_id": "visual_corn_2"}]})
    def pick(*_):
        station._completed_by_sku["corn"] = 1
        return {"placed": "visual_corn_2"}
    station.pick_and_place = MagicMock(side_effect=pick)
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
    def pick(*_):
        station._completed_by_sku["corn"] = 1
        return {"placed": "visual_corn_1"}
    station.pick_and_place = MagicMock(side_effect=pick)
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
    def pick(*_):
        station._completed_by_sku["corn"] = station._completed_by_sku.get("corn", 0) + 1
        return {"placed": "visual_corn_1"}
    station.pick_and_place = MagicMock(side_effect=pick)
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


def test_agent_finishes_both_corn_before_switching_sku_and_recovers_bad_steps():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._completed_by_sku = {"corn": 0, "carrot": 0}
    station.get_object_positions = MagicMock(side_effect=lambda _order, sku: {
        "detections": [{"detection_id": f"visual_{sku}_1"}]
    })

    def pick(_order, detection_id):
        sku = detection_id.split("_")[1]
        station._completed_by_sku[sku] += 1
        return {"placed": detection_id, "sku": sku}

    station.pick_and_place = MagicMock(side_effect=pick)
    station.finish_order = MagicMock(return_value={"status": "verified"})
    model = FakeModel([
        ("get_object_positions", {"sku": "corn"}),
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("get_object_positions", {"sku": "carrot"}),  # cannot skip second corn
        ("finish_order", {}),  # cannot claim partial order complete
        [("get_object_positions", {"sku": "corn"}), ("finish_order", {})],
        ("get_object_positions", {"sku": "corn"}),
        ("pick_and_place", {"detection_id": "visual_corn_1"}),
        ("get_object_positions", {"sku": "carrot"}),
        ("pick_and_place", {"detection_id": "visual_carrot_1"}),
        ("finish_order", {}),
    ])
    order = Order("ordered", "A", (OrderLine("corn", 2), OrderLine("carrot", 1)))
    assert station.run(order, model=model)["status"] == "COMPLETED"
    assert [call.args[1] for call in station.get_object_positions.call_args_list] == [
        "corn", "corn", "carrot"
    ]
    assert [call.args[1] for call in station.pick_and_place.call_args_list] == [
        "visual_corn_1", "visual_corn_1", "visual_carrot_1"
    ]
    assert station.finish_order.call_count == 1
    assert any(event["stage"] == "AGENT_RETRY" for event in station.events)


def test_agent_stops_after_five_empty_visual_attempts():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._completed_by_sku = {"tomato": 3}
    station.get_object_positions = MagicMock(return_value={
        "sku": "tomato", "detections": [], "remaining": 1,
    })
    model = FakeModel([("get_object_positions", {"sku": "tomato"})] * 5)
    order = Order("missing-tomato", "A", (OrderLine("tomato", 4),))

    result = station.run(order, model=model)

    assert result["status"] == "FAILED"
    assert "No tomato detected after 5 consecutive visual attempts" in result["error"]
    assert station.get_object_positions.call_count == 5
    assert [event["attempts"] for event in result["events"]
            if event["stage"] == "PERCEPTION_EMPTY"] == [1, 2, 3, 4, 5]
    assert [event["remaining"] for event in result["events"]
            if event["stage"] == "PERCEPTION_EXHAUSTED"] == [{"tomato": 1}]
    assert readable_error(result["error"]) == "连续 5 次视觉定位未找到番茄，还差 1 件；订单已停止。"
