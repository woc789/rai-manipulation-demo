"""The LLM may propose strategy, never arbitrary executable BT nodes."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from produce_order_workstation.agent import ProduceAgent
from produce_order_workstation.fulfillment_plan import (
    compile_tree, context_digest, planning_context, validate_plan, execution_contract_digest,
)
from produce_order_workstation.order import Order, OrderError, OrderLine


@pytest.fixture
def context():
    order = Order("order-1", "A", (OrderLine("corn", 2), OrderLine("apple", 1)))
    station = SimpleNamespace(_completed_by_sku={"corn": 1, "apple": 0},
        _allocated_bin_index=2, inventory=MagicMock())
    station.inventory.snapshot.return_value = {"epoch": 4, "items": [
        {"sku": "corn", "state": "RESERVED", "order_id": "order-1", "stale": False},
        {"sku": "apple", "state": "UNKNOWN", "order_id": "", "stale": True},
    ]}
    return planning_context(order, station, 5)


def proposal(context, items):
    return {"version": 1, "context_digest": context_digest(context), "items": items}


def test_passive_observation_age_does_not_invalidate_sku_only_strategy(context):
    aged = deepcopy(context)
    aged["inventory_observation"]["counts"]["corn"] = {
        "available": 0, "reserved": 0, "uncertain": 1
    }
    assert context_digest(aged) != context_digest(context)
    assert execution_contract_digest(aged) == execution_contract_digest(context)


@pytest.mark.parametrize("field,value", [
    ("bin_id", "A4"), ("remaining", {"corn": 0, "apple": 1}),
    ("completed", {"corn": 2, "apple": 0}),
    ("requested", {"corn": 3, "apple": 1}),
])
def test_business_changes_still_invalidate_strategy(context, field, value):
    changed = deepcopy(context)
    changed[field] = value
    assert execution_contract_digest(changed) != execution_contract_digest(context)


def test_scene_epoch_change_still_invalidates_strategy(context):
    changed = deepcopy(context)
    changed["inventory_observation"]["epoch"] += 1
    assert execution_contract_digest(changed) != execution_contract_digest(context)


def test_context_is_observation_not_hidden_simulator_truth(context):
    assert context["remaining"] == {"corn": 1, "apple": 1}
    assert context["bin_id"] == "A3"
    assert context["inventory_observation"]["counts"]["apple"]["uncertain"] == 1
    assert "position" not in json.dumps(context)


def test_agent_order_changes_compiled_execution_order(context):
    plan = proposal(context, [{"sku": "apple", "visual_attempts": 2},
                              {"sku": "corn", "visual_attempts": 5}])
    root = ET.fromstring(compile_tree(plan, context))
    assert [node.attrib["target_sku"] for node in root.iter("Capability")
            if node.attrib["command"] == "select"] == ["apple", "corn"]
    assert [node.attrib["num_attempts"] for node in root.iter("RetryUntilSuccessful")] == ["2", "5"]
    assert [node.attrib["command"] for node in root.iter("Capability")][-1] == "audit"


@pytest.mark.parametrize("items", [
    [{"sku": "corn", "visual_attempts": 2}],
    [{"sku": "corn", "visual_attempts": 2}, {"sku": "corn", "visual_attempts": 2}],
    [{"sku": "apple", "visual_attempts": 0}, {"sku": "corn", "visual_attempts": 2}],
    [{"sku": "apple", "visual_attempts": 6}, {"sku": "corn", "visual_attempts": 2}],
    [{"sku": "apple", "visual_attempts": 2}, {"sku": "corn", "visual_attempts": 2,
                                                    "command": "unsafe_motion"}],
])
def test_invalid_model_plan_rejected(context, items):
    with pytest.raises(OrderError):
        compile_tree(proposal(context, items), context)


def test_stale_context_rejected(context):
    plan = proposal(context, [{"sku": "apple", "visual_attempts": 2},
                              {"sku": "corn", "visual_attempts": 2}])
    changed = {**context, "completed": {"corn": 2, "apple": 0}}
    with pytest.raises(OrderError, match="different context"):
        validate_plan(plan, changed)


def test_agent_forces_one_plan_tool_and_validates_result(context):
    order = Order("order-1", "A", (OrderLine("corn", 2), OrderLine("apple", 1)))
    model = MagicMock()
    plan = proposal(context, [{"sku": "apple", "visual_attempts": 2},
                              {"sku": "corn", "visual_attempts": 3}])
    model.chat.completions.create.return_value.choices[0].message.tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name="propose_fulfillment_plan",
                                                  arguments=json.dumps(plan)))]
    agent = ProduceAgent(order, {"model": "test-model"}, MagicMock(), model=model)
    assert agent.plan_order(context) == plan
    assert model.chat.completions.create.call_args.kwargs["tool_choice"]["function"]["name"] == "propose_fulfillment_plan"
