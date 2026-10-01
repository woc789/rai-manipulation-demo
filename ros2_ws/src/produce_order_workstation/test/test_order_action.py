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
from produce_order_workstation.order import Order, OrderError, OrderLine, plan_placements, validate_order
from produce_order_workstation.order_server import order_from_goal
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


def test_scene_has_four_bins_and_mixed_order_independent_stock():
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


def test_preloaded_inventory_is_checked_when_order_begins():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.inspect_scene = MagicMock(return_value={"available": {"corn": 1}})
    with pytest.raises(OrderError, match="need 2, have 1"):
        station.begin_order(Order("short-stock", "A", (OrderLine("corn", 2),)))
    station.inspect_scene.return_value = {"available": {"corn": 4}}
    station.begin_order(Order("enough-stock", "A", (OrderLine("corn", 2),)))
    assert station._completed_by_sku == {"corn": 0}


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


def test_agent_tool_rejects_invented_entity_before_motion():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._source_entities = MagicMock(return_value={})
    station.move = MagicMock()
    with pytest.raises(OrderError, match="not reachable"):
        station.pick_and_place(Order("invalid", "A", (OrderLine("corn", 1),)), "invented")
    carrot = SimpleNamespace(name="stock_carrot_1", prefab_name="carrot")
    station._source_entities.return_value = {carrot.name: (carrot, (0.26, -0.27, 0.03))}
    with pytest.raises(OrderError, match="not needed"):
        station.pick_and_place(Order("wrong-sku", "A", (OrderLine("corn", 1),)), carrot.name)
    station.move.assert_not_called()


def test_agent_chosen_entity_runs_motion_and_audit(monkeypatch):
    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._completed_by_sku = {"corn": 0}
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    source = (0.29, -0.12, 0.04)
    entity = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    station._source_entities = MagicMock(return_value={entity.name: (entity, source)})
    station.slot_bin_position = MagicMock(return_value=slot)
    station.observe_after_place = MagicMock(return_value=[(*slot, 0.05)])
    station.move = MagicMock()
    order = Order("motion", "A", (OrderLine("corn", 1),))
    assert station.pick_and_place(order, entity.name)["bin"] == "A1"
    assert station.move.call_args_list == [
        call(source, grip="grab"),
        call((*slot, source[2]), grip="release", target_z=CONFIG["motion"]["bin_release_z_m"]),
        call((*slot, source[2]), grip="release", target_z=CONFIG["motion"]["bin_clearance_z_m"]),
    ]
    assert station.finish_order(order)["status"] == "verified"
    station.observe_after_place.return_value = [(slot[0] + 0.2, slot[1], 0.05)]
    with pytest.raises(OrderError, match="Final audit"):
        station.finish_order(order)


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


def test_agent_loop_uses_model_chosen_tool_and_entity():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.inspect_scene = MagicMock(return_value={"objects": [{"entity_id": "stock_corn_2", "sku": "corn"}]})
    station.pick_and_place = MagicMock(return_value={"placed": "stock_corn_2"})
    station.finish_order = MagicMock(return_value={"status": "verified"})
    model = FakeModel([("inspect_scene", {}), ("pick_and_place", {"entity_id": "stock_corn_2"}), ("finish_order", {})])
    result = station.run(Order("agent", "A", (OrderLine("corn", 1),)), model=model)
    assert result["status"] == "COMPLETED"
    assert station.pick_and_place.call_args.args[1] == "stock_corn_2"
    assert [item["stage"] for item in result["events"]].count("AGENT_TOOL_CALL") == 3


def test_agent_cannot_move_without_first_observation():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station.pick_and_place = MagicMock()
    result = station.run(Order("no-inspection", "A", (OrderLine("corn", 1),)), model=FakeModel([("pick_and_place", {"entity_id": "stock_corn_1"})]))
    assert result["status"] == "FAILED"
    assert "inspect" in result["error"]
    station.pick_and_place.assert_not_called()
