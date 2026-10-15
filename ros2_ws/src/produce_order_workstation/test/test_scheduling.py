"""Synthetic unit inputs only: no real orders, robot motion, or LLM calls."""

from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock
from types import MethodType
from concurrent.futures import Future

import pytest
import yaml

from db_support import make_resources, admit, complete
from db_support import make_inventory, reserve, reserve_order, mark_placed
from produce_order_workstation.order import Order, OrderLine
from produce_order_workstation.order_server import OrderActionServer
from produce_order_interfaces.srv import SubmitOrder, CancelQueuedOrder
from produce_order_interfaces.msg import OrderLine as LineMessage
from produce_order_interfaces.action import ExecuteOrder
from rclpy.action import GoalResponse


CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
)


def order(name, corn=1, apple=0):
    return Order(
        name,
        "A",
        tuple(
            OrderLine(sku, count)
            for sku, count in (("corn", corn), ("apple", apple))
            if count
        ),
    )


def stocked():
    book = make_inventory()
    book.observe("corn", "base", [(0.3, -0.1, 0.04), (0.3, 0.1, 0.04)])
    book.observe("apple", "base", [(0.4, 0.1, 0.04)])
    return book


def test_order_reservation_is_atomic_and_reports_unobserved_quantities():
    book = stocked()
    assert reserve_order(book, order("a", 2, 2))[1] == {"apple": 1}
    assert all(not item["order_id"] for item in book.snapshot()["items"])
    ids, missing = reserve_order(book, order("a", 2, 1))
    assert len(ids) == 3 and not missing
    assert reserve_order(book, order("b"))[1] == {"corn": 1}
    assert reserve_order(book, order("a", 2, 1))[0] == ids


def test_reobservation_preserves_reserved_identity_and_owner():
    book = stocked()
    ids, _ = reserve_order(book, order("a"))
    observed = book.observe("corn", "base", [(0.301, -0.1, 0.04)])
    assert observed == ids
    assert book.selectable(ids[0], "a")
    assert not book.selectable(ids[0], "b")
    reserve(book, ids[0], "a", refreshed_position=(0.302, -0.1, 0.04))
    assert book.snapshot()["items"][0]["state"] == "RESERVED"


def test_cross_sku_spatial_candidate_cannot_be_promised_twice():
    book = make_inventory()
    book.observe("corn", "base", [(0.3, 0.1, 0.04)])
    book.observe("apple", "base", [(0.301, 0.1, 0.04)])
    assert reserve_order(book, order("a", 1, 1))[1] == {"corn": 1, "apple": 1}
    assert all(not item["order_id"] for item in book.snapshot()["items"])
    assert reserve_order(book, order("a"))[1] == {"corn": 1}
    assert reserve_order(book, order("b", 0, 1))[1] == {"apple": 1}


def test_queue_explains_class_conflict_instead_of_claiming_stockout():
    book = make_inventory()
    book.observe("corn", "base", [(0.3, 0.1, 0.04)])
    book.observe("apple", "base", [(0.301, 0.1, 0.04)])
    queue = make_resources()
    queue.submit(order("ambiguous", 1, 1))
    bins = make_resources(CONFIG)
    assert queue.select_next(book) is None
    assert queue.get("ambiguous").state == "WAITING_STOCK"
    assert "不同商品" in queue.get("ambiguous").reason


def test_rejected_candidate_can_be_replaced_without_stealing_another_order():
    book = stocked()
    ids, _ = reserve_order(book, order("first"))
    book.reject_candidate(ids[0], "first")
    replacement, missing = reserve_order(book, order("first"))
    assert not missing and replacement != ids
    assert reserve_order(book, order("other"))[1] == {"corn": 1}
    with pytest.raises(ValueError):
        book.reject_candidate(replacement[0], "other")


def test_expired_reservation_is_not_free_stock_or_new_identity():
    tick = [0]
    book = make_inventory(clock=lambda: tick[0])
    original = book.observe("corn", "base", [(0.3, 0.1, 0.04)])[0]
    reserve_order(book, order("a"))
    tick[0] = 61
    assert book.snapshot()["items"][0]["state"] == "UNKNOWN"
    assert reserve_order(book, order("b"))[1] == {"corn": 1}
    assert book.observe("corn", "base", [(0.3, 0.1, 0.04)]) == [original]
    assert book.snapshot()["items"][0]["order_id"] == "a"


def test_priority_and_fifo_only_select_admissible_jobs():
    queue, book, bins = make_resources(), stocked(), make_resources(CONFIG)
    queue.submit(order("normal"), 0)
    queue.submit(order("urgent"), 10)
    assert queue.select_next(book).order.order_id == "urgent"
    # The selected job cannot be selected again or canceled as queued.
    with pytest.raises(ValueError):
        queue.cancel("urgent", book)
    queue.update("urgent", state="RUNNING")
    queue.submit(order("new-urgent"), 10)
    assert queue.get("urgent").state == "RUNNING"
    assert queue.select_next(book).order.order_id == "new-urgent"


def test_unknown_stock_waits_without_holding_partial_inventory():
    queue, book, bins = make_resources(), stocked(), make_resources(CONFIG)
    queue.submit(order("unobserved", 2, 2), 10)
    queue.submit(order("possible"), 0)
    assert queue.select_next(book).order.order_id == "possible"
    waiting = queue.get("unobserved")
    assert waiting.state == "WAITING_STOCK" and waiting.missing == {"apple": 1}
    assert "不等同于确认缺货" in waiting.reason


def test_waiting_pickup_bins_cannot_be_reused_until_confirmed():
    queue, book, bins = make_resources(), stocked(), make_resources(CONFIG)
    for index in range(4):
        current = order(f"done-{index}")
        bins.allocate(current)
        bins.finish_allocation(current.order_id, "COMPLETED", False)
    queue.submit(order("next"))
    assert queue.select_next(book) is None
    assert queue.get("next").state == "WAITING_BIN"
    assert all(not item["order_id"] for item in book.snapshot()["items"])
    bins.confirm_pickup("done-0", book)
    assert queue.select_next(book).order.order_id == "next"


def test_cancel_waiting_order_and_duplicate_limits():
    queue, book = make_resources(max_pending=1), stocked()
    queue.submit(order("a"))
    assert queue.submit(order("a")).order.order_id == "a"
    with pytest.raises(ValueError):
        queue.submit(order("b"))
    queue.cancel("a", book)
    assert not queue.pending()
    assert queue.submit(order("b")).state == "QUEUED"


def server_stub():
    resources = make_resources(CONFIG)
    return SimpleNamespace(
        _lock=Lock(),
        _busy=True,
        _scene_ready=True,
        _dispatch_order_id=None,
        _dispatch_handle=None,
        _dispatch_timed_out=False,
        _scan_requested=False,
        resources=resources,
        store=resources.store,
        config=CONFIG,
        workstation=SimpleNamespace(inventory=stocked()),
        report_path=lambda _: Path("/nonexistent-unit-report"),
        publish_status=MagicMock(),
        get_logger=MagicMock(),
    )


def test_offline_agent_keeps_order_queued_without_reserving_stock():
    server = server_stub()
    server._busy = False
    server._inventory_ready = True
    server._agent_dispatcher = SimpleNamespace(
        ready=lambda: False, error="model key missing"
    )
    current = order("waiting-agent")
    server.resources.submit(current)
    OrderActionServer.dispatch_pending(server)
    assert server._dispatch_order_id is None
    assert server.resources.get(current.order_id).state == "QUEUED"
    assert "model key missing" in server.resources.get(current.order_id).reason
    assert not server.store.rows("SELECT 1 FROM bin_allocations WHERE released IS NULL")


def test_moved_candidate_does_not_double_available_stock():
    book = make_inventory()
    old = book.observe("corn", "base", [(0.3, 0.1, 0.04)])[0]
    new = book.observe("corn", "base", [(0.36, 0.1, 0.04)])[0]
    assert old != new
    assert book.summary()["corn"] == {"UNKNOWN": 1, "AVAILABLE": 1}
    assert reserve_order(book, order("two", 2))[1] == {"corn": 1}
    assert all(not item["order_id"] for item in book.snapshot()["items"])


def test_moved_reserved_candidate_is_not_stolen_even_on_repeat_scan():
    book = make_inventory()
    old = book.observe("corn", "base", [(0.3, 0.1, 0.04)])[0]
    reserve_order(book, order("owner"))
    for _ in range(2):
        book.observe("corn", "base", [(0.36, 0.1, 0.04)])
        assert reserve_order(book, order("other"))[1] == {"corn": 1}
    rows = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert rows[old]["order_id"] == "owner" and rows[old]["state"] == "UNKNOWN"
    assert book.observe("corn", "base", [(0.301, 0.1, 0.04)]) == [old]
    assert book.selectable(old, "owner")


def test_empty_scan_retains_uncertain_history_and_partial_update_retains_other_items():
    book = stocked()
    book.observe("corn", "base", [(0.301, -0.1, 0.04)], complete_scan=False)
    assert book.summary()["corn"]["AVAILABLE"] == 2
    book.observe("corn", "base", [])
    assert book.summary()["corn"] == {"UNKNOWN": 2}
    assert book.summary()["apple"] == {"AVAILABLE": 1}


def watchdog_stub():
    server = server_stub()
    server.resources.submit(order("active"))
    reserve_order(server.workstation.inventory, order("active"))
    server._dispatch_order_id = "active"
    server._dispatch_handle = None
    server._dispatch_started = 100.0
    server._last_dispatch_feedback = 100.0
    server._accept_timeout, server._feedback_timeout, server._result_timeout = (
        15.0,
        180.0,
        900.0,
    )
    server._dispatch_timed_out = False
    server.get_logger = MagicMock()
    for name in (
        "_quarantine_dispatch",
        "_dispatch_failed",
        "_queue_result",
        "_expired_dispatch_reason",
    ):
        setattr(server, name, MethodType(getattr(OrderActionServer, name), server))
    return server


@pytest.mark.parametrize(
    "accepted,now,expected",
    [(False, 116.0, "接收响应"), (True, 281.0, "反馈停滞"), (True, 1001.0, "总时限")],
)
def test_watchdog_quarantines_without_releasing_or_repeating_motion(
    monkeypatch, accepted, now, expected
):
    server = watchdog_stub()
    handle = MagicMock() if accepted else None
    server._dispatch_handle = handle
    monkeypatch.setattr(
        "produce_order_workstation.order_server.time.monotonic", lambda: now
    )
    OrderActionServer.check_dispatch_timeout(server)
    assert server._dispatch_timed_out and not server._scene_ready
    assert server._state == "RECOVERY_REQUIRED" and expected in server._state_reason
    assert server.resources.get("active").state == "EXECUTION_UNCERTAIN"
    assert server._dispatch_order_id == "active"
    assert server.workstation.inventory.snapshot()["items"][0]["order_id"] == "active"
    OrderActionServer.check_dispatch_timeout(server)
    if handle:
        handle.cancel_goal_async.assert_called_once()


def test_feedback_refreshes_watchdog_without_unquarantining(monkeypatch):
    server = watchdog_stub()
    server._dispatch_handle = MagicMock()
    monkeypatch.setattr(
        "produce_order_workstation.order_server.time.monotonic", lambda: 200.0
    )
    OrderActionServer._queue_feedback(
        server, "active", SimpleNamespace(feedback=SimpleNamespace(completed=1))
    )
    OrderActionServer.check_dispatch_timeout(server)
    assert not server._dispatch_timed_out
    server._quarantine_dispatch("unit timeout", "active")
    OrderActionServer._queue_feedback(
        server, "active", SimpleNamespace(feedback=SimpleNamespace(completed=2))
    )
    assert server._dispatch_timed_out and not server._scene_ready


def test_late_acceptance_requests_cancel_and_late_result_does_not_restart_queue():
    server = watchdog_stub()
    server._quarantine_dispatch("unit timeout", "active")
    result_future = Future()
    handle = MagicMock(accepted=True)
    handle.get_result_async.return_value = result_future
    response = Future()
    response.set_result(handle)
    OrderActionServer._dispatch_response(server, response, "active")
    handle.cancel_goal_async.assert_called_once()
    with server.store.transaction() as db:
        db.execute("UPDATE order_lines SET completed=1 WHERE order_id='active'")
    result_future.set_result(
        SimpleNamespace(
            result=SimpleNamespace(status="COMPLETED", completed=1, error="")
        )
    )
    assert server.resources.get("active").state == "WAITING_PICKUP"
    assert server._dispatch_order_id is None
    assert server._state == "RECOVERY_REQUIRED" and not server._scene_ready


def test_stale_response_or_result_cannot_touch_new_dispatch():
    server = watchdog_stub()
    broken = Future()
    broken.set_exception(RuntimeError("old communication failed"))
    OrderActionServer._dispatch_response(server, broken, "old")
    OrderActionServer._queue_result(server, broken, "old")
    assert not server._dispatch_timed_out
    assert server._dispatch_order_id == "active"


def test_late_rejection_releases_only_confirmed_unstarted_order():
    server = watchdog_stub()
    server._quarantine_dispatch("unit timeout", "active")
    response = Future()
    response.set_result(SimpleNamespace(accepted=False))
    OrderActionServer._dispatch_response(server, response, "active")
    assert server._dispatch_order_id is None
    assert not server._scene_ready
    assert not any(
        item["order_id"] for item in server.workstation.inventory.snapshot()["items"]
    )


def test_ros_registration_accepts_while_busy_without_allocating_a_box(tmp_path):
    server = server_stub()
    # An exported JSON file is neither the order registry nor a business lock.
    exported = tmp_path / "unregistered.json"
    exported.write_text('{"status":"FAILED"}')
    server.report_path = lambda _: exported
    request = SubmitOrder.Request(
        order_id="unit",
        zone="A",
        priority=3,
        lines=[LineMessage(sku="corn", quantity=1)],
    )
    result = OrderActionServer.submit_callback(server, request, SubmitOrder.Response())
    assert result.success and result.state == "QUEUED"
    assert not server.store.rows("SELECT 1 FROM bin_allocations WHERE released IS NULL")
    assert server._scan_requested
    result = OrderActionServer.submit_callback(server, request, SubmitOrder.Response())
    assert result.success and len(server.resources.all()) == 1
    request.lines = [LineMessage(sku="corn", quantity=2)]
    result = OrderActionServer.submit_callback(server, request, SubmitOrder.Response())
    assert not result.success
    result = OrderActionServer.cancel_queued_callback(
        server, CancelQueuedOrder.Request(order_id="unit"), CancelQueuedOrder.Response()
    )
    assert result.success and server.resources.get("unit").state == "CANCELED"


def test_late_feedback_cannot_update_a_different_order():
    server = server_stub()
    server.resources.submit(order("normal"))
    server.resources.submit(order("urgent"), 10)
    server._dispatch_order_id = "urgent"
    message = SimpleNamespace(feedback=SimpleNamespace(completed=1))
    OrderActionServer._queue_feedback(server, "normal", message)
    assert server.resources.get("urgent").completed == 0
    OrderActionServer._queue_feedback(server, "urgent", message)
    OrderActionServer._queue_feedback(
        server, "urgent", SimpleNamespace(feedback=SimpleNamespace(completed=0))
    )
    assert (
        server.resources.get("urgent").completed == 0
    )  # feedback alone is not verified evidence


def test_execution_goal_cannot_change_registered_demand():
    server = server_stub()
    server._busy = False
    server.resources.submit(order("registered"))
    server._dispatch_order_id = "registered"
    server.get_logger = MagicMock()
    goal = ExecuteOrder.Goal(
        order_id="registered",
        zone="A",
        prepare_scene=True,
        lines=[LineMessage(sku="corn", quantity=2)],
    )
    assert OrderActionServer.goal_callback(server, goal) == GoalResponse.REJECT
    assert not server.store.rows("SELECT 1 FROM bin_allocations WHERE released IS NULL")


def test_direct_fulfillment_action_cannot_bypass_full_reservation():
    server = server_stub()
    server._busy = False
    server.get_logger = MagicMock()
    server.workstation.inventory = make_inventory()
    server.workstation.inventory.reset()
    goal = ExecuteOrder.Goal(
        order_id="direct",
        zone="A",
        prepare_scene=True,
        lines=[LineMessage(sku="corn", quantity=1)],
    )
    assert OrderActionServer.goal_callback(server, goal) == GoalResponse.REJECT
    assert not server.store.rows("SELECT 1 FROM bin_allocations WHERE released IS NULL")


@pytest.mark.parametrize(
    "busy,ready,dispatch",
    [(True, True, None), (False, False, None), (False, True, "current")],
)
def test_dispatch_never_bypasses_busy_recovery_or_active_goal(busy, ready, dispatch):
    server = server_stub()
    server._busy, server._scene_ready, server._dispatch_order_id = busy, ready, dispatch
    server.resources.submit(order("pending"))
    OrderActionServer.dispatch_pending(server)
    assert server.resources.get("pending").state == "QUEUED"
    assert not server.store.rows("SELECT 1 FROM bin_allocations WHERE released IS NULL")


def test_source_replacement_keeps_delivered_records_not_old_candidates():
    book = stocked()
    ids, _ = reserve_order(book, order("a"))
    book.transition(ids[0], "a", "CARRYING")
    mark_placed(book, ids[0], "a")
    book.invalidate_source()
    assert book.epoch == 1
    assert [item["state"] for item in book.snapshot()["items"]] == ["PLACED"]


def test_pause_preserves_bin_and_resumes_even_when_all_slots_are_occupied():
    book, bins, queue = stocked(), make_resources(CONFIG), make_resources()
    first = order("paused", 2)
    queue.submit(first)
    bins.allocate(first)
    ids, _ = reserve_order(book, first)
    book.transition(ids[0], first.order_id, "CARRYING")
    mark_placed(book, ids[0], first.order_id)
    bins.finish_allocation(first.order_id, "PAUSED", False)
    queue.finish(first.order_id, "PAUSED", 1, "urgent")
    for index in range(3):
        current = order(f"other-{index}")
        bins.allocate(current)
        bins.finish_allocation(current.order_id, "COMPLETED", False)
    assert not bins.can_reset()
    assert queue.select_next(book).order.order_id == "paused"
    assert bins.allocation("paused").bin_indices == (0,)
    assert reserve_order(book, first)[1] == {}


def test_urgent_yield_requires_higher_priority_and_complete_resources():
    server = server_stub()
    current = server.resources.submit(order("current"), 1)
    server.resources.update(current.order.order_id, state="RUNNING")
    server.resources.allocate(current.order)
    reserve_order(server.workstation.inventory, current.order)
    server.resources.submit(order("same-priority"), 1)
    assert not OrderActionServer._should_yield(server, "current")
    server.resources.submit(order("urgent-unobserved", 2), 10)
    assert not OrderActionServer._should_yield(server, "current")
    server.resources.submit(order("urgent-available", 0, 1), 9)
    assert OrderActionServer._should_yield(server, "current")
    assert not any(
        item["order_id"] == "urgent-available"
        for item in server.workstation.inventory.snapshot()["items"]
    )  # eligibility probe is read-only


@pytest.mark.parametrize("timed_out", [False, True])
def test_resume_uses_database_progress_without_an_in_memory_order_context(
    tmp_path, timed_out
):
    from produce_order_workstation.runtime import Workstation

    server = server_stub()
    server._busy = False
    station = Workstation(MagicMock(), CONFIG, store=server.store)
    station._scene_prepared = True
    first = order("resume", 2)
    complete(station, first, "corn", "already-placed")
    server.resources.finish_allocation("resume", "PAUSED", False)
    server.resources.finish("resume", "PAUSED", 1, "urgent")
    server._dispatch_order_id = "resume"
    server.workstation = station
    report = tmp_path / "resume.json"
    report.write_text('{"status":"PAUSED"}')
    server.report_path = lambda _: report
    station.ensure_order_bins = MagicMock()

    def finish_order(_):
        assert station._completed_by_sku == {"corn": 1}
        complete(station, first, "corn", "second")
        server._dispatch_timed_out = timed_out
        return dict(status="COMPLETED", completed=2, requested=2, error=None)

    station.run = MagicMock(side_effect=finish_order)
    server._publish_feedback = MagicMock()
    server._should_yield = lambda _: False
    goal = ExecuteOrder.Goal(
        order_id="resume",
        zone="A",
        prepare_scene=True,
        lines=[LineMessage(sku="corn", quantity=2)],
    )
    assert OrderActionServer.goal_callback(server, goal) == GoalResponse.ACCEPT
    result = OrderActionServer.execute_callback(
        server, MagicMock(request=goal, is_cancel_requested=False)
    )
    assert result.status == "COMPLETED"
    assert server.resources.get("resume").completed == 2
    assert len(server.resources.placements("resume")) == 2
    assert server.resources.bin("A1").state == "WAITING_PICKUP"
    assert server._scene_ready is not timed_out


def test_replenishment_preserves_delivered_entities_and_order_box(monkeypatch):
    from produce_order_workstation import runtime
    from produce_order_workstation.runtime import Workstation

    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._scene_bridge = MagicMock()
    old = SimpleNamespace(name="old_source", prefab_name="corn")
    delivered = SimpleNamespace(name="delivered", prefab_name="apple")
    box = SimpleNamespace(name="order_bin_A1", prefab_name="toy_box")
    station._scene_bridge.spawned_entities = [old, delivered, box]
    complete(station, order("delivered-order", 0, 1), "apple", "delivered")
    station.synchronize_scene = MagicMock()
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    station.replenish_source(Path(__file__).parents[1] / "config/demo_scene.yaml")
    station._scene_bridge._despawn_entity.assert_called_once_with(old)
    spawned = [
        call.args[0] for call in station._scene_bridge._spawn_entity.call_args_list
    ]
    assert len(spawned) == 16
    assert all(
        entity.prefab_name in CONFIG["aliases"] and "_refill_1" in entity.name
        for entity in spawned
    )
    station.synchronize_scene.assert_called_once()


def test_cancel_stale_untouched_reservation_does_not_leak_owner():
    tick = [0]
    book = make_inventory(clock=lambda: tick[0])
    identity = book.observe("corn", "base", [(0.3, 0.1, 0.04)])[0]
    reserve_order(book, order("a"))
    tick[0] = 61
    book.release_order("a")
    assert book.snapshot()["items"][0]["state"] == "UNKNOWN"
    assert not book.snapshot()["items"][0]["order_id"]
    assert book.observe("corn", "base", [(0.3, 0.1, 0.04)]) == [identity]
    assert not reserve_order(book, order("b"))[1]


def test_discard_paused_requires_explicit_confirmation_and_cleanup_success(tmp_path):
    server = server_stub()
    server._busy = False
    job = order("partial", 2)
    server.resources.submit(job)
    server.resources.allocate(job)
    with server.store.transaction() as db:
        db.execute("UPDATE order_lines SET completed=1 WHERE order_id='partial'")
    server.resources.finish_allocation("partial", "PAUSED", False)
    server.resources.finish("partial", "PAUSED", 1, "urgent")
    server.workstation.collect_order = MagicMock(
        side_effect=RuntimeError("delete failed")
    )
    server.report_path = lambda _: tmp_path / "partial.json"
    result = OrderActionServer.cancel_queued_callback(
        server,
        CancelQueuedOrder.Request(order_id="partial"),
        CancelQueuedOrder.Response(),
    )
    assert not result.success
    result = OrderActionServer._discard_partial(
        server, "partial", CancelQueuedOrder.Response()
    )
    assert not result.success and server.resources.bin("A1").state == "PAUSED"
    server.workstation.collect_order.side_effect = None
    result = OrderActionServer._discard_partial(
        server, "partial", CancelQueuedOrder.Response()
    )
    assert result.success and server.resources.bin("A1").state == "FREE"
    assert server.resources.get("partial").state == "CANCELED"
    assert (tmp_path / "partial.discard.json").exists()


@pytest.mark.parametrize("verified", [True, False])
def test_actual_tool_yields_only_after_retreat_and_verified_inventory_commit(
    monkeypatch, verified
):
    from produce_order_workstation.runtime import Workstation
    from produce_order_workstation.order import OrderPaused, OrderError

    monkeypatch.setattr("produce_order_workstation.runtime.time.sleep", lambda _: None)
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    station._allocated_bin_index = 0
    source = (0.29, -0.12, 0.04)
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    entity = SimpleNamespace(name="unit-corn", prefab_name="corn")
    station._visual_candidates = {"visual": ("corn", source)}
    station.detect = MagicMock(return_value=[source])
    station._source_entities = MagicMock(return_value={entity.name: (entity, source)})
    station._planning_scene = MagicMock()
    station.slot_bin_position = MagicMock(return_value=slot)
    station.entity_position = MagicMock(
        return_value=(source[0], source[1], source[2] + 0.1)
    )
    station.observe_after_place = MagicMock(
        return_value=[(*slot, 0.05)] if verified else [(2.0, 2.0, 0.05)]
    )
    station.move = MagicMock()
    station.yield_requested = MagicMock(return_value=True)
    admit(station, order("normal", 2))
    with pytest.raises(OrderPaused if verified else OrderError):
        station.pick_and_place(order("normal", 2), "visual")
    assert [call.kwargs["step"] for call in station.move.call_args_list] == [
        "grab",
        "lift",
        "place",
        "retreat",
    ]
    if verified:
        station.yield_requested.assert_called_once()
        assert station._completed_by_sku == {"corn": 1}
        assert station.inventory.snapshot()["items"][0]["state"] == "PLACED"
    else:
        station.yield_requested.assert_not_called()
        assert station._completed_by_sku == {"corn": 0}


def test_collected_snapshot_cannot_retain_waiting_pickup_reason():
    queue = make_resources()
    job = queue.submit(Order("picked-up", "A", (OrderLine("corn", 1),)))
    queue.update(job.order.order_id, state="COLLECTED", reason="已确认取货，箱位已释放")
    row = queue.snapshot_orders()[0]
    assert row["state"] == "COLLECTED"
    assert "已经确认取货" in row["state_label"]
    assert "等待取货" not in row["reason"]
