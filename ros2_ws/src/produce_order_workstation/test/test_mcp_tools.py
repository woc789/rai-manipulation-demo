"""Real HTTP/MCP exchange with a fake capability backend; no paid API or motion."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from produce_order_workstation.mcp_client import WorkstationMCPClient
from produce_order_workstation.mcp_server import WorkstationMCPServer
from produce_order_workstation.order import Order, OrderLine, OrderError, OrderCancelled, OrderPaused, PerceptionMismatch


@pytest.fixture
def station():
    return SimpleNamespace(config={"operation_mode": "fulfillment"},
        _completed_by_sku={}, _visual_candidates={"visual_1": ("corn", (.4, .1, .04))},
        check_cancel=MagicMock(), event=MagicMock(),
        get_object_positions=MagicMock(return_value={"detections": [{"detection_id": "visual_1"}]}),
        pick_and_place=MagicMock(return_value={"placed": "visual_1"}),
        finish_order=MagicMock(return_value={"status": "verified"}))


@contextmanager
def connection(station):
    order = Order("mcp-test", "A", (OrderLine("corn", 1),))
    with WorkstationMCPServer(station, order) as server:
        with WorkstationMCPClient(server.url, server.token) as client:
            yield server, client


def test_discover_call_and_read_resource_cross_real_http(station):
    with connection(station) as (server, client):
        assert server.url.startswith("http://127.0.0.1:")
        tools = {tool["function"]["name"]: tool["function"] for tool in client.tools}
        assert set(tools) == {"get_object_positions", "pick_and_place", "finish_order"}
        assert tools["get_object_positions"]["parameters"]["required"] == ["sku"]
        assert client.context()["order_id"] == "mcp-test"
        assert client.call("get_object_positions", {"sku": "corn"})["detections"]
        assert client.call("pick_and_place", {"detection_id": "visual_1"})["placed"] == "visual_1"
        assert client.call("finish_order", {})["status"] == "verified"
        station.pick_and_place.assert_called_once()
        assert station.pick_and_place.call_args.args[0].order_id == "mcp-test"
        station._completed_by_sku["corn"] = 1
        assert client.context()["completed"] == {"corn": 1}
        assert "MCP_TOOL_CALL" in [args.args[1] for args in station.event.call_args_list]


@pytest.mark.parametrize("name,arguments", [
    ("unknown", {}), ("get_object_positions", {}),
    ("get_object_positions", {"sku": 3}),
    ("get_object_positions", {"sku": "corn", "order_id": "another-order"}),
    ("pick_and_place", {"detection_id": "visual_1", "x": .4}),
])
def test_unknown_and_invalid_arguments_never_reach_workstation(station, name, arguments):
    with connection(station) as (_, client):
        with pytest.raises(OrderError):
            client.call(name, arguments)
        station.get_object_positions.assert_not_called()
        station.pick_and_place.assert_not_called()


def test_server_requires_fresh_perception_even_for_independent_client(station):
    with connection(station) as (_, client):
        with pytest.raises(OrderError, match="before moving"):
            client.call("pick_and_place", {"detection_id": "visual_1"})
        client.call("get_object_positions", {"sku": "corn"})
        client.call("pick_and_place", {"detection_id": "visual_1"})
        with pytest.raises(OrderError, match="stale"):
            client.call("pick_and_place", {"detection_id": "visual_1"})
        station.pick_and_place.assert_called_once()


def test_unrequested_sku_and_cancelled_order_do_not_run_vision(station):
    with connection(station) as (_, client):
        with pytest.raises(OrderError, match="not needed"):
            client.call("get_object_positions", {"sku": "tomato"})
        station.check_cancel.side_effect = OrderCancelled("Operator cancelled")
        with pytest.raises(OrderCancelled, match="Operator cancelled"):
            client.call("get_object_positions", {"sku": "corn"})
        station.get_object_positions.assert_not_called()


@pytest.mark.parametrize("error", [
    PerceptionMismatch("wrong class", code="CLASS_CONFLICT", reobserve=False),
    OrderPaused("Yield to urgent order"), OrderCancelled("Cancelled at boundary"),
    OrderError("Plan failed"),
])
def test_domain_errors_survive_mcp_without_changing_failure_branches(station, error):
    with connection(station) as (_, client):
        client.call("get_object_positions", {"sku": "corn"})
        station.pick_and_place.side_effect = error
        with pytest.raises(type(error), match=str(error)) as caught:
            client.call("pick_and_place", {"detection_id": "visual_1"})
        if isinstance(error, PerceptionMismatch):
            assert caught.value.code == error.code
            assert caught.value.reobserve == error.reobserve
        station.pick_and_place.assert_called_once()


def test_endpoint_rejects_requests_without_order_capability(station):
    with connection(station) as (server, _):
        with httpx.Client(trust_env=False) as http:
            assert http.post(server.url, json={}).status_code == 401
            assert http.post(server.url, json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        station.get_object_positions.assert_not_called()


def test_two_clients_cannot_run_robot_tools_concurrently(station):
    started, release = threading.Event(), threading.Event()
    def perceive(*_):
        started.set()
        assert release.wait(5)
        return {"detections": [{"detection_id": "visual_1"}]}
    station.get_object_positions.side_effect = perceive
    with connection(station) as (server, first):
        with WorkstationMCPClient(server.url, server.token) as second:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(first.call, "get_object_positions", {"sku": "corn"})
                try:
                    assert started.wait(5)
                    with pytest.raises(OrderError, match="in flight"):
                        second.call("get_object_positions", {"sku": "corn"})
                finally:
                    release.set()
                assert future.result(timeout=5)["detections"]
            station.get_object_positions.assert_called_once()


def test_session_shutdown_closes_threads_and_port(station):
    with connection(station) as (server, client):
        url = server.url
    assert not server._thread.is_alive() and not client._thread.is_alive()
    with httpx.Client(trust_env=False, timeout=1) as http:
        with pytest.raises(httpx.ConnectError):
            http.post(url, json={})
    with pytest.raises(OrderError, match="closed"):
        client.call("finish_order", {})


def test_http_timeout_never_retries_motion_or_releases_backend_early(station):
    started, release, closing = threading.Event(), threading.Event(), threading.Event()
    def pick(*_):
        started.set()
        assert release.wait(5)
        return {"placed": "visual_1"}
    station.pick_and_place.side_effect = pick
    order = Order("timeout-test", "A", (OrderLine("corn", 1),))
    with WorkstationMCPServer(station, order) as server:
        with WorkstationMCPClient(server.url, server.token, timeout_s=.3) as client:
            client.call("get_object_positions", {"sku": "corn"})
            with pytest.raises(OrderError, match="do not automatically repeat"):
                client.call("pick_and_place", {"detection_id": "visual_1"})
            assert started.is_set()
            def close():
                closing.set()
                server.close()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(close)
                try:
                    assert closing.wait(2)
                    assert not future.done()
                finally:
                    release.set()
                future.result(timeout=5)
            station.pick_and_place.assert_called_once()
