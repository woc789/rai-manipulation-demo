from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest
import yaml

from produce_order_workstation.business import BusinessCoordinator
from db_support import make_resources
from db_support import make_inventory
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.order_server import OrderActionServer
from produce_order_workstation.database import WorkcellDatabase


CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
)


@pytest.fixture
def server(tmp_path):
    store = WorkcellDatabase(tmp_path / "test.db")
    s = SimpleNamespace(
        config=CONFIG,
        store=store,
        resources=make_resources(CONFIG, store=store),
        workstation=SimpleNamespace(inventory=make_inventory(store=store)),
        _lock=Lock(),
        _state="READY",
        _state_reason="ready",
        _busy=False,
        _dispatch_order_id=None,
        _startup_review=False,
        _scan_requested=False,
    )
    s.business = BusinessCoordinator(s)
    yield s
    store.close()


def add_order(s, state="QUEUED"):
    s.resources.submit(Order("A", "A", (OrderLine("corn", 2),)))
    return s.resources.update("A", state=state)


def test_operation_intent_survives_reopen_and_cannot_replay(tmp_path):
    path = tmp_path / "state.db"
    store = WorkcellDatabase(path)
    store.begin_operation("A", "PICK", "op-1")
    store.close()
    store = WorkcellDatabase(path)
    assert store.operations()[0]["state"] == "INTENT"
    with pytest.raises(ValueError, match="never replay"):
        store.begin_operation("A", "PICK", "op-1")
    store.finish_operation("op-1", "VERIFIED", {"completed": 1})
    with pytest.raises(ValueError):
        store.finish_operation("op-1", "VERIFIED")
    store.close()


def test_relational_order_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    store = WorkcellDatabase(path)
    resources = make_resources(CONFIG, store=store)
    resources.submit(Order("A", "A", (OrderLine("corn", 2),)), priority=3)
    store.close()
    store = WorkcellDatabase(path)
    assert make_resources(CONFIG, store=store).get("A").priority == 3
    store.close()


def test_business_tools_cross_mcp_but_cannot_approve_or_move(server):
    from produce_order_workstation.business_mcp import BusinessMCPServer
    from produce_order_workstation.mcp_client import WorkstationMCPClient
    from produce_order_workstation.order import OrderError

    job = add_order(server)
    with BusinessMCPServer(server.business) as gateway:
        with WorkstationMCPClient(gateway.url, gateway.token) as client:
            names = {tool["function"]["name"] for tool in client.tools}
            assert names == {
                "query_orders",
                "query_inventory",
                "propose_fulfillment_plan",
                "request_order_change",
                "request_replenishment",
                "explain_task_status",
            }
            assert client.call("query_orders", {})["orders"][0]["order_id"] == "A"
            proposal = client.call(
                "request_order_change", {"order_id": "A", "quantities": {"corn": 1}}
            )
            assert not proposal["applied"] and job.total == 2
            with pytest.raises(OrderError):
                client.call(
                    "approve_request", {"request_id": proposal["request"]["request_id"]}
                )
            with pytest.raises(OrderError):
                client.call("pick_and_place", {"detection_id": "invented"})
            server.business.call(
                "approve_request", {"request_id": proposal["request"]["request_id"]}
            )
            assert server.resources.get("A").total == 1


def test_request_resolution_and_order_change_are_one_transaction(server):
    add_order(server)
    request = server.store.create_request({"kind": "test"})
    with server.store.transaction():
        server.resources.update("A", reason="committed")
        server.store.resolve_request(request["request_id"], "APPROVED")
    with pytest.raises(ValueError):
        with server.store.transaction():
            server.resources.update("A", reason="must rollback")
            server.store.resolve_request(request["request_id"], "APPROVED")
    assert server.resources.get("A").reason == "committed"


def test_agent_proposal_does_not_change_order(server):
    job = add_order(server)
    result = server.business.call(
        "request_order_change", {"order_id": "A", "quantities": {"corn": 1}}
    )
    assert not result["applied"]
    assert job.order.total_items == 2
    server.business.call(
        "approve_request", {"request_id": result["request"]["request_id"]}
    )
    assert server.resources.get("A").order.total_items == 1
    assert server._scan_requested


@pytest.mark.parametrize(
    "state", ["DISPATCHING", "RUNNING", "PAUSED", "WAITING_PICKUP", "FAILED"]
)
def test_cannot_modify_started_order(server, state):
    add_order(server, state)
    with pytest.raises(ValueError, match="unstarted"):
        server.business.call(
            "request_order_change", {"order_id": "A", "quantities": {"corn": 1}}
        )


def test_approval_revalidates_order_state(server):
    job = add_order(server)
    result = server.business.call(
        "request_order_change", {"order_id": "A", "quantities": {"corn": 1}}
    )
    server.resources.update("A", state="DISPATCHING")
    with pytest.raises(ValueError, match="started"):
        server.business.call(
            "approve_request", {"request_id": result["request"]["request_id"]}
        )
    assert job.order.total_items == 2


def test_old_process_request_cannot_be_approved(server):
    add_order(server)
    req = server.business.call(
        "request_order_change", {"order_id": "A", "quantities": {"corn": 1}}
    )["request"]
    server.business = BusinessCoordinator(server)
    with pytest.raises(ValueError, match="another process"):
        server.business.call("approve_request", {"request_id": req["request_id"]})


def test_rejection_and_duplicate_confirmation(server):
    job = add_order(server)
    req = server.business.call(
        "request_order_change", {"order_id": "A", "quantities": {"corn": 1}}
    )["request"]
    server.business.call("reject_request", {"request_id": req["request_id"]})
    assert job.order.total_items == 2
    with pytest.raises(ValueError):
        server.business.call("approve_request", {"request_id": req["request_id"]})


@pytest.mark.parametrize("quantity", [-1, 1.5, True, "2"])
def test_invalid_quantities(server, quantity):
    add_order(server)
    with pytest.raises(ValueError):
        server.business.call(
            "request_order_change", {"order_id": "A", "quantities": {"corn": quantity}}
        )


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
    store = WorkcellDatabase(tmp_path / "restore.db")
    resources = make_resources(CONFIG, store=store)
    resources.submit(Order("queued", "A", (OrderLine("corn", 1),)), priority=1)
    placed = Order("placed", "A", (OrderLine("apple", 1),))
    resources.submit(placed)
    resources.allocate(placed)
    with store.transaction() as db:
        db.execute("UPDATE order_lines SET completed=1 WHERE order_id='placed'")
    resources.allocation_state("placed", "WAITING_PICKUP")
    resources.update("placed", state="WAITING_PICKUP")
    s = SimpleNamespace(store=store, resources=resources, _startup_review=False)
    OrderActionServer._restore_business_state(s)
    assert resources.get("queued").state == "QUEUED"
    assert resources.get("placed").state == "RECOVERY_REQUIRED"
    assert resources.get("placed").completed == 1 and s._startup_review
    assert resources.bin("A1").state == "RECOVERY_REQUIRED"
    store.close()
