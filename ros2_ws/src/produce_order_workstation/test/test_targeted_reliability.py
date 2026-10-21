"""Targeted reliability regression probes. Fake skills, real ledgers/SQLite."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock, Thread, Event
from types import SimpleNamespace as S
from unittest.mock import MagicMock
import time
import pytest
import yaml
from produce_order_workstation.order import (
    Order,
    OrderLine,
    OrderCancelled,
    PerceptionMismatch,
)
from db_support import make_resources
from db_support import make_inventory, reserve_order, mark_placed
from produce_order_workstation.business import BusinessCoordinator
from produce_order_workstation.database import WorkcellDatabase
from produce_order_workstation.execution_bridge import ExecutionBridge
from produce_order_workstation.mcp_server import (
    WorkstationMCPServer,
    _request_capability,
)
from produce_order_workstation.order_server import OrderActionServer
from produce_order_interfaces.srv import FulfillmentStep

CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
)


def order(name, sku="corn", count=1):
    return Order(name, "A", (OrderLine(sku, count),))


@pytest.fixture
def business(tmp_path):
    store = WorkcellDatabase(tmp_path / "business.db")
    s = S(
        config=CONFIG,
        resources=make_resources(CONFIG, store=store),
        workstation=S(inventory=make_inventory(store=store)),
        _lock=Lock(),
        _state="READY",
        _state_reason="ready",
        _busy=False,
        _dispatch_order_id=None,
        _startup_review=False,
        _scan_requested=False,
        store=store,
    )
    s.business = BusinessCoordinator(s)
    yield s
    store.close()


@pytest.fixture
def bridge(tmp_path):
    b = ExecutionBridge.__new__(ExecutionBridge)
    b._lock = Lock()
    b.station = MagicMock()
    b.station._completed_by_sku = {"corn": 0}
    b.station.physical_state_uncertain = False
    b.station._visual_candidates = {"v1": ("corn", (0.4, 0.1, 0.04))}
    b.station._agent_dispatcher.execute.return_value = "v1"
    b.station.pick_and_place.side_effect = (
        lambda *_: b.station._completed_by_sku.update(
            corn=b.station._completed_by_sku["corn"] + 1
        )
    )
    b.server = S(store=WorkcellDatabase(tmp_path / "steps.db"))
    b._active = dict(
        token="admitted",
        order=order("task", count=2),
        sequence=0,
        cache={},
        cancelled=False,
        terminal=False,
        last_candidate="",
        alternatives=False,
    )
    yield b
    b.server.store.close()


def step(b, command, seq=0, identifier="", token="admitted"):
    return b.step(
        FulfillmentStep.Request(
            session_id=token, sequence=seq, command=command, detection_id=identifier
        ),
        FulfillmentStep.Response(),
    )


def test_eight_orders_four_bins_wait_and_pickup_unblocks():
    q = make_resources()
    bins = make_resources(CONFIG)
    book = make_inventory()
    book.observe("corn", "base", [(0.3, i * 0.15, 0.04) for i in range(4)])
    book.observe("apple", "base", [(0.6, i * 0.15, 0.04) for i in range(4)])
    for i in range(8):
        q.submit(order(f"job-{i}", "corn" if i % 2 == 0 else "apple"))
    for _ in range(4):
        job = q.select_next(book)
        assert job is not None
        bins.allocate(job.order)
        for item in book.snapshot()["items"]:
            if item["order_id"] == job.order.order_id:
                book.transition(item["item_id"], job.order.order_id, "CARRYING")
                mark_placed(book, item["item_id"], job.order.order_id)
        bins.finish_allocation(job.order.order_id, "COMPLETED", False)
        q.finish(job.order.order_id, "COMPLETED", 1, "waiting pickup")
    assert q.select_next(book) is None
    assert all(j.state == "WAITING_BIN" for j in q.pending())
    bins.confirm_pickup("job-0", book)
    assert q.select_next(book).order.order_id == "job-4"


def test_32_orders_concurrently_reserve_only_eight_items():
    book = make_inventory()
    book.observe("corn", "base", [(0.3, i * 0.12, 0.04) for i in range(8)])
    with ThreadPoolExecutor(max_workers=16) as pool:
        result = list(
            pool.map(lambda i: reserve_order(book, order(f"r-{i}")), range(32))
        )
    assert sum(not missing for _, missing in result) == 8
    rows = book.snapshot()["items"]
    assert len(rows) == 8
    assert len({r["order_id"] for r in rows}) == 8


def test_two_four_sku_mixed_orders_cannot_share_same_inventory():
    q = make_resources()
    bins = make_resources(CONFIG)
    book = make_inventory()
    skus = ["corn", "carrot", "apple", "tomato"]
    for i, sku in enumerate(skus):
        book.observe(sku, "base", [(0.3 + i * 0.12, 0.1, 0.04)])
    lines = tuple(OrderLine(sku, 1) for sku in skus)
    q.submit(Order("normal", "A", lines), priority=0)
    q.submit(Order("urgent", "A", lines), priority=5)
    assert q.select_next(book).order.order_id == "urgent"
    bins.allocate(q.get("urgent").order)
    assert q.select_next(book) is None
    assert q.get("normal").state == "WAITING_STOCK"
    assert len(book.snapshot()["items"]) == 4
    assert {r["order_id"] for r in book.snapshot()["items"]} == {"urgent"}


def test_model_timeout_never_moves_and_releases_agent_busy(monkeypatch):
    from produce_order_workstation import agent_service
    from produce_order_interfaces.action import ExecuteAgent
    from produce_order_interfaces.msg import OrderLine as LineMessage
    from threading import Event

    model = MagicMock()
    model.chat.completions.create.side_effect = TimeoutError("injected model timeout")
    client = MagicMock()
    client.context.return_value = {
        "order_id": "api-outage",
        "zone": "A",
        "requested": {"corn": 1},
        "completed": {},
    }
    client.call.return_value = {
        "detections": [{"detection_id": "v1", "position": [0.3, 0.1, 0.04]}]
    }
    context = MagicMock()
    context.__enter__.return_value = client
    monkeypatch.setattr(
        agent_service, "WorkstationMCPClient", lambda *_args, **_kw: context
    )
    service = S(
        config=CONFIG,
        model=model,
        _busy=True,
        _active_order="api-outage",
        get_logger=MagicMock(),
        _lock=Lock(),
        _stopping=Event(),
        publish_status=MagicMock(),
    )
    goal = ExecuteAgent.Goal(
        order_id="api-outage",
        zone="A",
        lines=[LineMessage(sku="corn", quantity=1)],
        mcp_url="http://127.0.0.1:1/mcp",
        mcp_token="isolated",
        mode="select",
    )
    handle = MagicMock(request=goal, is_cancel_requested=False)
    result = agent_service.ProduceAgentService.execute(service, handle)
    assert result.status == "FAILED" and "injected model timeout" in result.error
    assert not service._busy and service._active_order == ""
    client.call.assert_called_once_with("get_object_positions", {"sku": "corn"})
    handle.abort.assert_called_once()


def test_cancel_waiting_order_releases_its_reserved_stock():
    q = make_resources()
    book = make_inventory()
    book.observe("corn", "base", [(0.3, 0.1, 0.04)])
    a = q.submit(order("cancel-me"))
    reserve_order(book, a.order)
    q.cancel("cancel-me", book)
    assert (
        q.get("cancel-me").state == "CANCELED"
        and not reserve_order(book, order("next"))[1]
    )


def test_expired_observation_is_not_promised_to_another_order():
    tick = [0]
    book = make_inventory(clock=lambda: tick[0])
    book.observe("corn", "base", [(0.3, 0.1, 0.04)])
    reserve_order(book, order("owner"))
    tick[0] = 61
    assert reserve_order(book, order("other"))[1] == {"corn": 1}
    assert book.snapshot()["items"][0]["order_id"] == "owner"


def test_agent_proposal_needs_confirmation_and_running_order_cannot_change(business):
    job = business.resources.submit(order("amend", count=2))
    req = business.business.call(
        "request_order_change", {"order_id": "amend", "quantities": {"corn": 1}}
    )["request"]
    assert job.total == 2
    business.resources.update("amend", state="RUNNING")
    with pytest.raises(ValueError):
        business.business.call("approve_request", {"request_id": req["request_id"]})
    assert job.total == 2


def test_failed_approval_write_must_leave_original_order(business):
    business.resources.submit(order("amend", count=2))
    req = business.business.call(
        "request_order_change", {"order_id": "amend", "quantities": {"corn": 1}}
    )["request"]

    def fail(*_):
        raise OSError("injected disk failure")

    business.resources.replace_order = fail
    with pytest.raises(OSError):
        business.business.call("approve_request", {"request_id": req["request_id"]})
    assert business.resources.get("amend").total == 2


def test_rejected_maintenance_request_can_be_retried_after_cause_removed(business):
    req = business.business.call("request_replenishment", {"sku": "corn"})["request"]

    def reject(_, r):
        r.success = False
        r.message = "maintenance not ready"

    business.replenish_callback = reject
    assert not business.business.call(
        "approve_request", {"request_id": req["request_id"]}
    )["applied"]

    def accept(_, r):
        r.success = True
        r.message = "maintenance admitted"

    business.replenish_callback = accept
    assert business.business.call("approve_request", {"request_id": req["request_id"]})[
        "applied"
    ]


def test_expired_execution_session_cannot_move(bridge):
    assert step(bridge, "pick", identifier="v1", token="old-session").status == "FAILED"
    bridge.station.pick_and_place.assert_not_called()


def test_duplicate_step_returns_result_without_second_pick(bridge):
    assert step(bridge, "select").status == "SUCCESS"
    assert step(bridge, "pick", 1, "v1").status == "SUCCESS"
    assert step(bridge, "pick", 1, "v1").status == "SUCCESS"
    assert bridge.station.pick_and_place.call_count == 1


def test_verified_pick_result_remains_queryable_after_event_failure(bridge):
    assert step(bridge, "select").status == "SUCCESS"
    bridge.station.event.side_effect = OSError("event failed after verified pick")
    with pytest.raises(OSError):
        step(bridge, "pick", 1, "v1")
    bridge.station.event.side_effect = None
    assert step(bridge, "pick", 1, "v1").status == "SUCCESS"
    assert bridge.station.pick_and_place.call_count == 1


def test_mcp_and_bt_share_one_skill_execution_lease(bridge):
    bridge._active["last_candidate"] = "v1"
    first = Event()
    release = Event()
    lock = Lock()
    state = {"active": 0, "peak": 0}

    def pick(*_):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        first.set()
        release.wait(timeout=3)
        with lock:
            bridge.station._completed_by_sku["corn"] += 1
            state["active"] -= 1
        return {"placed": "v1"}

    bridge.station.pick_and_place.side_effect = pick
    mcp = WorkstationMCPServer(bridge.station, bridge._active["order"])
    mcp._perceived = True

    def call_mcp():
        marker = _request_capability.set(mcp.token)
        try:
            mcp._execute("pick_and_place", {"detection_id": "v1"})
        finally:
            _request_capability.reset(marker)

    a = Thread(target=lambda: step(bridge, "pick", 0, "v1"))
    a.start()
    assert first.wait(2)
    b = Thread(target=call_mcp)
    b.start()
    time.sleep(0.15)
    release.set()
    a.join(3)
    b.join(3)
    assert not a.is_alive() and not b.is_alive()
    assert state["peak"] == 1, f"two entry points overlapped: {state['peak']}"


def test_uncertain_physical_result_is_not_retryable(bridge):
    step(bridge, "select")

    def fail(*_):
        bridge.station.physical_state_uncertain = True
        raise PerceptionMismatch(
            "motion outcome unknown", code="UNKNOWN", reobserve=True
        )

    bridge.station.pick_and_place.side_effect = fail
    assert step(bridge, "pick", 1, "v1").status == "FAILED"
    assert bridge.server.store.operations()[0]["state"] == "UNCERTAIN"


def test_cancel_before_pick_does_not_move(bridge):
    step(bridge, "select")
    bridge.station.check_cancel.side_effect = OrderCancelled("cancelled")
    assert step(bridge, "pick", 1, "v1").status == "CANCELLED"
    bridge.station.pick_and_place.assert_not_called()


def test_restart_partial_order_keeps_progress_and_quarantines_coordinates(tmp_path):
    store = WorkcellDatabase(tmp_path / "restart.db")
    resources = make_resources(CONFIG, store=store)
    inventory = make_inventory(store=store)
    job = order("partial", count=2)
    resources.submit(job)
    resources.allocate(job)
    with store.transaction() as db:
        db.execute("UPDATE order_lines SET legacy_completed=1 WHERE order_id='partial'")
    inventory.observe("corn", "base", [(0.3, 0.1, 0.04)])
    resources.update("partial", state="RUNNING")
    store.begin_operation("partial", "PICK", "unconfirmed")
    s = S(store=store, resources=resources, _startup_review=False)
    OrderActionServer._restore_business_state(s)
    assert (
        resources.get("partial").completed == 1
        and resources.get("partial").state == "RECOVERY_REQUIRED"
    )
    assert resources.bin("A1").order_id == "partial" and s._startup_review
    assert inventory.snapshot()["items"][0]["state"] == "UNKNOWN"
    store.close()


@pytest.mark.parametrize(
    "reliable", [True, False], ids=["production-clock-qos", "best-effort-control"]
)
def test_clock_health_callback_receives_best_effort_publisher(reliable):
    import rclpy
    from rclpy.qos import QoSProfile, ReliabilityPolicy
    from rosgraph_msgs.msg import Clock

    rclpy.init()
    n = rclpy.create_node("target_clock_probe")
    s = S(_sim_seen=None, _sim_clock_value=None)
    q = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
    from produce_order_workstation.order_server import CLOCK_QOS

    subscription_qos = CLOCK_QOS if reliable else q
    n.create_subscription(
        Clock,
        "/target_assessment/clock",
        lambda m: OrderActionServer._sim_clock(s, m),
        subscription_qos,
    )
    p = n.create_publisher(Clock, "/target_assessment/clock", q)
    try:
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            p.publish(Clock())
            rclpy.spin_once(n, timeout_sec=0.03)
        assert s._sim_seen is not None, "health callback never receives clock"
    finally:
        n.destroy_node()
        rclpy.shutdown()


def test_approval_failure_rolls_back_reservations_and_is_retryable(business):
    business.resources.submit(order("amend", count=2))
    book = business.workstation.inventory
    book.clock = lambda: 100.0
    book.observe("corn", "base", [(0.3, 0.1, 0.04), (0.5, 0.1, 0.04)])
    reserve_order(book, order("amend", count=2))
    before = book.snapshot()["items"]
    req = business.business.call(
        "request_order_change", {"order_id": "amend", "quantities": {"corn": 1}}
    )["request"]
    resolve = business.store.resolve_request

    def fail(*_):
        raise OSError("disk unavailable")

    business.store.resolve_request = fail
    with pytest.raises(OSError):
        business.business.call("approve_request", {"request_id": req["request_id"]})
    assert book.snapshot()["items"] == before and not business._scan_requested
    assert business.store.request(req["request_id"])["state"] == "PENDING"
    business.store.resolve_request = resolve
    assert business.business.call("approve_request", {"request_id": req["request_id"]})[
        "applied"
    ]
    assert business.resources.get("amend").total == 1


def test_verified_result_can_be_recovered_without_memory_cache(bridge):
    step(bridge, "select")
    assert step(bridge, "pick", 1, "v1").status == "SUCCESS"
    bridge._active["cache"].clear()
    assert step(bridge, "pick", 1, "v1").status == "SUCCESS"
    assert bridge.station.pick_and_place.call_count == 1


def test_operation_commit_failure_blocks_replaying_motion(bridge):
    step(bridge, "select")

    def fail(*_):
        raise OSError("journal unavailable")

    bridge.server.store.finish_operation = fail
    assert step(bridge, "pick", 1, "v1").status == "FAILED"
    assert step(bridge, "pick", 1, "v1").status == "FAILED"
    assert bridge.station.pick_and_place.call_count == 1
    assert bridge.station.physical_state_uncertain


def test_maintenance_fast_completion_cannot_be_downgraded(business):
    req = business.business.call("request_replenishment", {})["request"]

    def start(kind, identifier, response):
        business.store.execution_result(identifier, "SUCCEEDED")
        response.success, response.message = True, "done"

    business.start_maintenance = start
    assert (
        business.business.call("approve_request", {"request_id": req["request_id"]})[
            "execution_state"
        ]
        == "SUCCEEDED"
    )
    with pytest.raises(ValueError):
        business.business.call("approve_request", {"request_id": req["request_id"]})


def test_interrupted_maintenance_is_unknown_not_retryable(business):
    req = business.business.call("request_replenishment", {})["request"]
    business.store.admit_maintenance(req["request_id"])
    business.store.execution_result(req["request_id"], "RUNNING")
    business.business = BusinessCoordinator(business)
    assert business.store.request(req["request_id"])["execution_state"] == "UNKNOWN"
    with pytest.raises(ValueError):
        business.business.call("approve_request", {"request_id": req["request_id"]})


@pytest.mark.parametrize("mode", ["never_seen", "disconnected", "frozen", "healthy"])
def test_clock_missing_disconnected_and_frozen_are_detected(mode):
    now = time.monotonic()
    s = S(
        config=CONFIG,
        _clock_started=now - 300,
        _sim_seen=now,
        _sim_progress_seen=now,
        _sim_clock_value=(1, 0),
    )
    if mode == "never_seen":
        s._sim_seen = None
    if mode == "disconnected":
        s._sim_seen = now - 20
    if mode == "frozen":
        s._sim_progress_seen = now - 20
    assert bool(OrderActionServer._clock_problem(s)) == (mode != "healthy")


def test_restart_never_promotes_operation_json_into_new_progress(tmp_path):
    store = WorkcellDatabase(tmp_path / "gap.db")
    resources = make_resources(CONFIG, store=store)
    resources.submit(order("partial", count=2))
    resources.update("partial", state="RUNNING")
    identifier = store.begin_operation("partial", "PICK_AND_PLACE")
    store.finish_operation(identifier, "VERIFIED", {"completed_by_sku": {"corn": 1}})
    s = S(store=store, resources=resources, _startup_review=False)
    OrderActionServer._restore_business_state(s)
    assert resources.get("partial").completed == 0
    assert resources.get("partial").state == "RECOVERY_REQUIRED"
    store.close()


@pytest.mark.parametrize("failure", [None, "physical", "database"])
def test_scene_reset_clears_review_only_after_physical_success(business, failure):
    job = business.resources.submit(order("old", count=2))
    with business.store.transaction() as db:
        db.execute("UPDATE order_lines SET legacy_completed=1 WHERE order_id='old'")
    business.resources.update("old", state="RECOVERY_REQUIRED", review_required=True)
    business._startup_review = True
    business.scene_path = Path("unused")
    business.workstation = MagicMock()
    business.get_logger = MagicMock()
    business.publish_status = MagicMock()
    business._set_state = MagicMock()
    if failure == "physical":
        business.workstation.prepare_scene.side_effect = RuntimeError("reset failed")
    if failure == "database":
        with business.store.transaction() as db:
            db.execute(
                "CREATE TRIGGER reset_fault BEFORE UPDATE ON orders BEGIN SELECT RAISE(ABORT,'reset database failure'); END"
            )
    req = business.business.call("request_scene_reset", {})["request"]
    business.store.admit_maintenance(req["request_id"])
    OrderActionServer._recover_scene(business, req["request_id"])
    assert business.store.request(req["request_id"])["execution_state"] == (
        "FAILED" if failure else "SUCCEEDED"
    )
    assert business._startup_review == bool(failure)
    assert business.resources.get("old").state == (
        "RECOVERY_REQUIRED" if failure else "FAILED"
    )
    assert business.resources.get("old").completed == 1


def test_bt_mcp_session_refuses_direct_physical_tools(bridge):
    mcp = WorkstationMCPServer(bridge.station)
    with mcp.order_session(bridge._active["order"], allow_motion=False):
        marker = _request_capability.set(mcp.token)
        try:
            result = mcp._execute("pick_and_place", {"detection_id": "v1"})
            assert "error" in result
            bridge.station.pick_and_place.assert_not_called()
        finally:
            _request_capability.reset(marker)


def test_execution_gate_supports_nested_same_thread_skills(bridge):
    from produce_order_workstation.execution_gate import (
        execution_gate,
        serialized_skill,
    )

    @serialized_skill
    def inner(station):
        return "done"

    with execution_gate(bridge.station).hold():
        assert inner(bridge.station) == "done"


def test_clock_reappearing_does_not_release_quarantined_order():
    now = time.monotonic()
    s = S(
        config=CONFIG,
        _clock_started=now - 300,
        _sim_seen=now - 20,
        _sim_progress_seen=now - 20,
        _state="RUNNING",
        _scene_ready=True,
        _dispatch_order_id="carrying",
        _lock=Lock(),
        publish_status=MagicMock(),
        _quarantine_dispatch=MagicMock(),
    )
    s._clock_problem = lambda: OrderActionServer._clock_problem(s)
    OrderActionServer._check_simulator(s)
    assert s._state == "SCENE_OFFLINE" and not s._scene_ready
    s._quarantine_dispatch.assert_called_once()
    s._sim_seen = s._sim_progress_seen = time.monotonic()
    OrderActionServer._check_simulator(s)
    assert s._state == "SCENE_OFFLINE" and not s._scene_ready
