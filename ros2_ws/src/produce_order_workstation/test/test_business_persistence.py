from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest
import yaml

from produce_order_workstation.business import BusinessCoordinator
from produce_order_workstation.fulfillment import BinManager
from produce_order_workstation.inventory import InventoryLedger
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.order_server import OrderActionServer
from produce_order_workstation.persistence import WorkcellStore
from produce_order_workstation.scheduling import OrderQueue


CONFIG = yaml.safe_load((Path(__file__).parents[1]/"config/workstation.yaml").read_text())


@pytest.fixture
def server(tmp_path):
    s = SimpleNamespace(config=CONFIG, queue=OrderQueue(), bin_manager=BinManager(CONFIG),
        workstation=SimpleNamespace(inventory=InventoryLedger()), _lock=Lock(),
        _state="READY", _state_reason="ready", _busy=False, _dispatch_order_id=None,
        _startup_review=False, _scan_requested=False, store=WorkcellStore(tmp_path/"test.db"),
        _persist_locked=lambda **_: None)
    s.business = BusinessCoordinator(s)
    def persist(**kwargs):
        s.store.save({"orders": s.queue.snapshot()}, **kwargs)
    s._persist_locked = persist
    yield s
    s.store.close()


def add_order(s, state="QUEUED"):
    job = s.queue.submit(Order("A", "A", (OrderLine("corn", 2),)))
    job.state = state
    return job


def test_operation_intent_survives_reopen_and_cannot_replay(tmp_path):
    path = tmp_path/"state.db"
    store = WorkcellStore(path)
    store.begin_operation("A", "PICK", "op-1")
    store.close()
    store = WorkcellStore(path)
    assert store.operations()[0]["state"] == "INTENT"
    with pytest.raises(ValueError, match="never replay"):
        store.begin_operation("A", "PICK", "op-1")
    store.finish_operation("op-1", "VERIFIED", {"completed": 1})
    with pytest.raises(ValueError):
        store.finish_operation("op-1", "VERIFIED")
    store.close()


def test_snapshot_roundtrip(tmp_path):
    store = WorkcellStore(tmp_path/"state.db")
    snapshot = {"orders": [{"order_id": "A", "completed": 1}], "bins": [], "schema": 1}
    store.save(snapshot)
    store.save(snapshot)
    assert store.load() == snapshot
    store.close()


def test_business_tools_cross_mcp_but_cannot_approve_or_move(server):
    from produce_order_workstation.business_mcp import BusinessMCPServer
    from produce_order_workstation.mcp_client import WorkstationMCPClient
    from produce_order_workstation.order import OrderError
    job = add_order(server)
    with BusinessMCPServer(server.business) as gateway:
        with WorkstationMCPClient(gateway.url, gateway.token) as client:
            names = {tool["function"]["name"] for tool in client.tools}
            assert names == {"query_orders", "query_inventory", "propose_fulfillment_plan",
                "request_order_change", "request_replenishment", "explain_task_status"}
            assert client.call("query_orders", {})["orders"][0]["order_id"] == "A"
            proposal = client.call("request_order_change", {"order_id": "A", "quantities": {"corn": 1}})
            assert not proposal["applied"] and job.total == 2
            with pytest.raises(OrderError):
                client.call("approve_request", {"request_id": proposal["request"]["request_id"]})
            with pytest.raises(OrderError):
                client.call("pick_and_place", {"detection_id": "invented"})
            server.business.call("approve_request", {"request_id": proposal["request"]["request_id"]})
            assert job.total == 1


def test_request_resolution_and_snapshot_are_one_transaction(tmp_path):
    store = WorkcellStore(tmp_path/"state.db")
    request = store.create_request({"kind":"test"})
    store.save({"version":1},resolution=(request["request_id"],"APPROVED"))
    assert store.requests()[0]["state"] == "APPROVED"
    with pytest.raises(ValueError):
        store.save({"version":2},resolution=(request["request_id"],"APPROVED"))
    assert store.load() == {"version":1}
    store.close()


def test_agent_proposal_does_not_change_order(server):
    job = add_order(server)
    result = server.business.call("request_order_change", {"order_id": "A", "quantities": {"corn": 1}})
    assert not result["applied"]
    assert job.order.total_items == 2
    server.business.call("approve_request", {"request_id": result["request"]["request_id"]})
    assert job.order.total_items == 1
    assert server._scan_requested


@pytest.mark.parametrize("state", ["DISPATCHING", "RUNNING", "PAUSED", "WAITING_PICKUP", "FAILED"])
def test_cannot_modify_started_order(server, state):
    add_order(server, state)
    with pytest.raises(ValueError, match="unstarted"):
        server.business.call("request_order_change", {"order_id": "A", "quantities": {"corn": 1}})


def test_approval_revalidates_order_state(server):
    job = add_order(server)
    result = server.business.call("request_order_change", {"order_id": "A", "quantities": {"corn": 1}})
    job.state = "DISPATCHING"
    with pytest.raises(ValueError, match="started"):
        server.business.call("approve_request", {"request_id": result["request"]["request_id"]})
    assert job.order.total_items == 2


def test_old_process_request_cannot_be_approved(server):
    add_order(server)
    req = server.business.call("request_order_change", {"order_id": "A", "quantities": {"corn": 1}})["request"]
    server.business = BusinessCoordinator(server)
    with pytest.raises(ValueError, match="another process"):
        server.business.call("approve_request", {"request_id": req["request_id"]})


def test_rejection_and_duplicate_confirmation(server):
    job = add_order(server)
    req = server.business.call("request_order_change", {"order_id": "A", "quantities": {"corn": 1}})["request"]
    server.business.call("reject_request", {"request_id": req["request_id"]})
    assert job.order.total_items == 2
    with pytest.raises(ValueError):
        server.business.call("approve_request", {"request_id": req["request_id"]})


@pytest.mark.parametrize("quantity", [-1, 1.5, True, "2"])
def test_invalid_quantities(server, quantity):
    add_order(server)
    with pytest.raises(ValueError):
        server.business.call("request_order_change", {"order_id": "A", "quantities": {"corn": quantity}})


def test_replenishment_request_does_not_spawn_or_seed_stock(server):
    server.business.call("request_replenishment", {"sku": "corn"})
    assert server.workstation.inventory.snapshot()["items"] == []
    assert server.store.requests()[0]["state"] == "PENDING"


def test_repeated_proposal_returns_original_pending_request(server):
    first = server.business.call("request_replenishment", {"sku": "corn"})
    second = server.business.call("request_replenishment", {"sku": "corn"})
    assert first["request"]["request_id"] == second["request"]["request_id"]
    assert len(server.store.requests()) == 1


def test_restart_preserves_queue_and_quarantines_physical_tasks(tmp_path):
    store = WorkcellStore(tmp_path/"restore.db")
    store.save({"orders": [
        dict(order_id="queued", zone="A", lines=[dict(sku="corn",quantity=1)], priority=1,
             state="QUEUED", reason="waiting", completed=0),
        dict(order_id="placed", zone="A", lines=[dict(sku="apple",quantity=1)], priority=0,
             state="WAITING_PICKUP", reason="done", completed=1)],
        "allocations": [dict(order_id="placed",bin_indices=[0], state="WAITING_PICKUP")]})
    s = OrderActionServer.__new__(OrderActionServer)
    s.store, s.queue, s.bin_manager = store, OrderQueue(), BinManager(CONFIG)
    s._startup_review = False
    OrderActionServer._restore_business_state(s)
    assert s.queue.jobs["queued"].state == "QUEUED"
    assert s.queue.jobs["placed"].state == "RECOVERY_REQUIRED"
    assert s.queue.jobs["placed"].completed == 1
    assert s.bin_manager.bins["A1"].state == "RECOVERY_REQUIRED"
    assert s._startup_review
    store.close()
