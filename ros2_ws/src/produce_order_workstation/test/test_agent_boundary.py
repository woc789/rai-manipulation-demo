"""The decision loop runs without Workstation, ROS, or starting an MCP server."""

import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from produce_order_workstation import agent as agent_module
from produce_order_workstation.agent import ProduceAgent
from produce_order_workstation.order import Order, OrderCancelled, OrderError, OrderLine


CONFIG = yaml.safe_load((Path(__file__).parents[1] / "config/workstation.yaml").read_text())["agent"]


class MemoryToolClient:
    """A test double implementing the tool-client contract, with no workstation."""

    def __init__(self):
        self.tools = [{"type": "function", "function": {"name": name}}
                      for name in ("get_object_positions", "pick_and_place", "finish_order")]
        self.calls = []
        self.completed = {}

    def context(self):
        return {"mode": "fulfillment", "completed": dict(self.completed),
                "candidate_ids": ["visual_corn"]}

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "get_object_positions":
            return {"detections": [{"detection_id": "visual_corn"}]}
        if name == "pick_and_place":
            self.completed["corn"] = self.completed.get("corn", 0) + 1
            return {"placed": "visual_corn"}
        return {"status": "verified"}


def model_for(calls):
    steps = iter(calls)
    def create(**_):
        name, arguments = next(steps)
        tool = SimpleNamespace(id=name, function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments)))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=None, tool_calls=[tool]))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=MagicMock(side_effect=create))))


def order():
    return Order("independent-agent", "A", (OrderLine("corn", 1),))


def test_agent_runs_from_injected_contracts_without_workstation():
    client, events = MemoryToolClient(), MagicMock()
    calls = [("get_object_positions", {"sku": "corn"}),
             ("pick_and_place", {"detection_id": "visual_corn"}), ("finish_order", {})]
    model = model_for(calls)
    runner = ProduceAgent(order(), CONFIG, client, model=model, event_sink=events)
    assert not hasattr(runner, "workstation")
    runner.run()
    assert client.calls == calls and client.completed == {"corn": 1}
    assert model.chat.completions.create.call_args.kwargs["tools"] is client.tools
    assert events.call_args.args[:2] == (order().order_id, "AGENT_TOOL_RESULT")


def test_agent_cancellation_before_start_never_queries_or_calls_model():
    client, model = MagicMock(), model_for([])
    runner = ProduceAgent(order(), CONFIG, client, model=model, cancel_requested=lambda: True)
    with pytest.raises(OrderCancelled):
        runner.run()
    client.context.assert_not_called()
    client.call.assert_not_called()
    model.chat.completions.create.assert_not_called()


def test_cancel_while_model_waits_discards_late_tool_choice():
    client, cancel = MemoryToolClient(), [False]
    model = model_for([("get_object_positions", {"sku": "corn"})])
    original = model.chat.completions.create.side_effect
    def respond(**kwargs):
        cancel[0] = True
        return original(**kwargs)
    model.chat.completions.create.side_effect = respond
    runner = ProduceAgent(order(), CONFIG, client, model=model, cancel_requested=lambda: cancel[0])
    with pytest.raises(OrderCancelled):
        runner.run()
    assert client.calls == []


def test_disconnected_tool_does_not_fall_back_or_retry_physical_action():
    client = MemoryToolClient()
    client.call = MagicMock(side_effect=OrderError("MCP disconnected"))
    runner = ProduceAgent(order(), CONFIG, client,
        model=model_for([("get_object_positions", {"sku": "corn"})]))
    with pytest.raises(OrderError, match="MCP disconnected"):
        runner.run()
    client.call.assert_called_once()


def test_model_factory_receives_only_agent_config(monkeypatch):
    model, config = model_for([]), deepcopy(CONFIG)
    factory = MagicMock(return_value=model)
    monkeypatch.setattr(agent_module, "create_model", factory)
    runner = ProduceAgent(order(), config, MemoryToolClient())
    factory.assert_called_once_with(config)
    config["max_tool_calls_per_item"] = 999
    assert runner.config["max_tool_calls_per_item"] == CONFIG["max_tool_calls_per_item"]


def test_agent_source_cannot_access_workstation_or_start_transport():
    tree = ast.parse(Path(agent_module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "workstation"
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {"runtime", "mcp_server", "mcp_client"}
