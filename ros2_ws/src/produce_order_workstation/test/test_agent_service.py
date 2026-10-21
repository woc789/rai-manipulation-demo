"""Runtime admission and result handling; no model API or user's ROS domain."""

from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from rclpy.action import GoalResponse
import yaml

from produce_order_workstation import agent_service
from produce_order_workstation.agent_service import ProduceAgentService
from produce_order_workstation.order import OrderCancelled, OrderError, OrderPaused


CONFIG = yaml.safe_load((Path(__file__).parents[1] / "config/workstation.yaml").read_text())


def request(**updates):
    values = dict(order_id="admitted", zone="A", lines=[SimpleNamespace(sku="corn", quantity=1)],
                  mcp_url="http://127.0.0.1:8000/mcp", mcp_token="private-capability")
    return SimpleNamespace(**{**values, **updates})


def runtime():
    return SimpleNamespace(config=CONFIG, model=MagicMock(), _busy=False,
        _active_order="", _lock=Lock(), _stopping=Event(), publish_status=MagicMock(),
        get_logger=MagicMock())


@pytest.mark.parametrize("url", ["http://example.com/mcp", "file:///tmp/robot",
    "http://127.0.0.1:8000/other", "http://user:password@127.0.0.1:8000/mcp",
    "http://127.0.0.1:8000/mcp?token=secret"])
def test_only_local_mcp_endpoint_is_admitted(url):
    service = runtime()
    assert ProduceAgentService.accept(service, request(mcp_url=url)) == GoalResponse.REJECT
    assert not service._busy


def test_admission_is_serial_and_missing_model_is_rejected():
    service = runtime()
    assert ProduceAgentService.accept(service, request()) == GoalResponse.ACCEPT
    assert service._busy and service._active_order == "admitted"
    assert ProduceAgentService.accept(service, request(order_id="second")) == GoalResponse.REJECT
    service._busy = False
    service.model = None
    assert ProduceAgentService.accept(service, request()) == GoalResponse.REJECT


@pytest.mark.parametrize("error,status,terminal", [(None, "COMPLETED", "succeed"),
    (OrderError("vision failed"), "FAILED", "abort"),
    (OrderPaused("yield"), "PAUSED", "succeed"),
    (OrderCancelled("cancel"), "CANCELLED", "canceled")])
def test_action_preserves_terminal_result_and_releases_runtime(monkeypatch, error, status, terminal):
    service = runtime()
    service._busy = True
    client = MagicMock()
    client.context.return_value = {"order_id": "admitted", "zone": "A", "requested": {"corn": 1}}
    client.tools = []
    connection = MagicMock()
    connection.__enter__.return_value = client
    monkeypatch.setattr(agent_service, "WorkstationMCPClient", lambda *_, **__: connection)
    loop = MagicMock()
    loop.run.side_effect = error
    factory = MagicMock(return_value=loop)
    monkeypatch.setattr(agent_service, "ProduceAgent", factory)
    handle = MagicMock(request=request(), is_cancel_requested=False)
    if status == "CANCELLED":
        # Cancellation arrives during the tool loop, not before connection.
        def cancel():
            handle.is_cancel_requested = True
            raise error
        loop.run.side_effect = cancel
    result = ProduceAgentService.execute(service, handle)
    assert result.status == status
    getattr(handle, terminal).assert_called_once()
    assert not service._busy and not service._active_order
    connection.__exit__.assert_called_once()
    assert "private-capability" not in str(handle.publish_feedback.call_args_list)


def test_order_context_mismatch_never_starts_model_loop(monkeypatch):
    service = runtime()
    client = MagicMock()
    client.context.return_value = {"order_id": "other", "zone": "A", "requested": {"corn": 1}}
    connection = MagicMock()
    connection.__enter__.return_value = client
    monkeypatch.setattr(agent_service, "WorkstationMCPClient", lambda *_, **__: connection)
    factory = MagicMock()
    monkeypatch.setattr(agent_service, "ProduceAgent", factory)
    handle = MagicMock(request=request(), is_cancel_requested=False)
    result = ProduceAgentService.execute(service, handle)
    assert result.status == "FAILED" and "does not match" in result.error
    factory.assert_not_called()
