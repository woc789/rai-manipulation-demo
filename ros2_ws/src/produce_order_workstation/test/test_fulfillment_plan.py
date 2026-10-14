"""The LLM may propose strategy, never arbitrary executable BT nodes."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from produce_order_workstation.agent import ProduceAgent
from produce_order_workstation.fulfillment_plan import (
    compile_tree, context_digest, planning_context, validate_plan,
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
