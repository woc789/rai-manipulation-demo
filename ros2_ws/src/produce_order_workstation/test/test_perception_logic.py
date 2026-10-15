"""Regression cases for candidate handling, with no live robot or API calls."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from threading import Lock

import pytest
import yaml

from produce_order_workstation import runtime
from db_support import admit, complete, make_inventory, reserve, reserve_order
from produce_order_workstation.order import (
    Order,
    OrderError,
    OrderLine,
    validate_order,
    MotionPlanRejected,
    PerceptionMismatch,
)
from produce_order_workstation.runtime import Workstation
from db_support import make_resources

CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "config/workstation.yaml").read_text()
)


def station():
    return Workstation(MagicMock(), deepcopy(CONFIG))


def test_workstation_passes_association_gate_to_inventory():
    assert station().inventory.radius == CONFIG["perception"]["association_radius_m"]


def test_visible_alternative_exchanges_reservation_without_overpromising():
    book = make_inventory()
    first, alternative = book.observe(
        "corn", "base", [(0.3, -0.3, 0.03), (0.3, 0.3, 0.03)]
    )
    order = Order("owner", "A", (OrderLine("corn", 1),))
    reserve(book, first, order.order_id)
    book.select_for_order(alternative, order)
    items = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert items[first]["state"] == "AVAILABLE" and not items[first]["order_id"]
    assert items[alternative]["order_id"] == "owner"
    assert sum(item["order_id"] == "owner" for item in items.values()) == 1


def test_alternative_cannot_steal_or_replace_unknown_physical_state():
    book = make_inventory()
    first, alternative = book.observe(
        "corn", "base", [(0.3, -0.3, 0.03), (0.3, 0.3, 0.03)]
    )
    order = Order("owner", "A", (OrderLine("corn", 1),))
    reserve(book, first, "other")
    with pytest.raises(ValueError):
        book.select_for_order(first, order)
    book.release_order("other")
    reserve(book, first, "owner")
    book.observe("corn", "base", [])
    book.observe("corn", "base", [(0.3, 0.3, 0.03)])
    with pytest.raises(ValueError, match="untouched"):
        book.select_for_order(alternative, order)


def test_refresh_obeys_configured_association_radius():
    book = make_inventory(association_radius=0.02)
    item = book.observe("corn", "base", [(0.3, 0.0, 0.03)])[0]
    with pytest.raises(ValueError, match="association"):
        reserve(book, item, "owner", refreshed_position=(0.33, 0.0, 0.03))


def test_cross_sku_depth_jitter_cannot_double_promise_same_surface_location():
    book = make_inventory(association_radius=0.065)
    book.observe("apple", "base", [(0.3, 0.0, 0.03)])
    book.observe("tomato", "base", [(0.3, 0.0, 0.10)])
    ids, missing = reserve_order(
        book, Order("a", "A", (OrderLine("apple", 1), OrderLine("tomato", 1)))
    )
    assert not ids and missing == {"tomato": 1}


def test_active_grasp_does_not_expire_and_failure_releases_execution_lease():
    tick = [1.0]
    book = make_inventory(ttl=2, clock=lambda: tick[0])
    item = book.observe("corn", "base", [(0.3, 0.0, 0.03)])[0]
    reserve(book, item, "owner")
    book.begin_execution(item, "owner")
    book.release_order("owner")
    with pytest.raises(ValueError, match="in flight"):
        book.select_for_order(item, Order("owner", "A", (OrderLine("corn", 1),)))
    tick[0] = 10
    assert book.snapshot()["items"][0]["state"] == "RESERVED"
    book.transition(item, "owner", "CARRYING")
    book.finish_failed("owner", True)
    assert not book.has_active_execution()
    assert book.snapshot()["items"][0]["state"] == "UNKNOWN"


def test_surface_height_shift_preserves_reserved_xy_identity():
    book = make_inventory(association_radius=0.065)
    item = book.observe("corn", "base", [(0.3, 0.1, 0.02)])[0]
    reserve(book, item, "owner")
    assert book.observe("corn", "base", [(0.35, 0.1, 0.065)]) == [item]
    assert book.selectable(item, "owner") and not book.selectable(item, "other")


def test_lost_reserved_item_does_not_quarantine_unrelated_stock():
    book = make_inventory(association_radius=0.065)
    old = book.observe("corn", "base", [(0.3, -0.3, 0.03)])[0]
    reserve(book, old, "owner")
    new = book.observe("corn", "base", [(0.3, 0.3, 0.03)])[0]
    items = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert items[old]["state"] == "UNKNOWN" and items[old]["order_id"] == "owner"
    assert items[new]["state"] == "AVAILABLE"


def test_local_ambiguous_replacement_stays_unavailable():
    book = make_inventory(association_radius=0.065)
    old = book.observe("corn", "base", [(0.3, 0.0, 0.03)])[0]
    reserve(book, old, "owner")
    new = book.observe("corn", "base", [(0.38, 0.0, 0.03)])[0]
    assert not book.selectable(new, "other")


def test_rejection_cooldown_does_not_blacklist_neighbor_and_expires(monkeypatch):
    tick = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: tick[0])
    work = station()
    work._visual_candidates = {"bad": ("corn", (0.3, 0.1, 0.03))}
    work._reject_visual_candidate(
        Order("a", "A", (OrderLine("corn", 1),)),
        "bad",
        "corn",
        (0.3, 0.1, 0.03),
        "CLASS_CONFLICT",
        {},
    )
    assert work._recently_rejected("corn", (0.3, 0.1, 0.03))
    assert not work._recently_rejected("corn", (0.33, 0.1, 0.03))
    tick[0] += 21
    assert not work._recently_rejected("corn", (0.3, 0.1, 0.03))


def test_rejected_alias_does_not_stop_fallback():
    work = station()
    work._position_tool = MagicMock()
    work._position_tool.invoke.side_effect = [
        "Centroid(x=0.3, y=0.1, z=0.03)",
        "Centroid(x=0.4, y=0.1, z=0.03)",
    ]
    work._record_perception_pipeline = MagicMock()
    work._candidate_rejections = {
        "bad": dict(
            sku="corn",
            position=(0.3, 0.1, 0.03),
            time=runtime.time.monotonic(),
            code="CLASS_CONFLICT",
        )
    }
    assert work.detect("corn") == [(0.4, 0.1, 0.03)]
    assert work._position_tool.invoke.call_count == 2


def test_absent_box_does_not_exclude_source_but_present_box_does():
    work = station()
    work._scene_prepared = True
    work._scene_bridge = SimpleNamespace(spawned_entities=[])
    zone = CONFIG["zones"]["A"]
    point = (*zone["slots"][0], 0.03)
    assert work._choose_source([point], zone) == point
    work._scene_bridge.spawned_entities = [SimpleNamespace(name="order_bin_A1")]
    with pytest.raises(OrderError):
        work._choose_source([point], zone)


def test_fresh_visual_target_skips_redundant_inference_without_skipping_interlock():
    work = station()
    point = (0.30, 0.1, 0.03)
    entity = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    work._allocated_bin_index = 0
    work._visual_candidates = {"fresh": ("corn", point)}
    work._visual_observed_at = runtime.time.monotonic()
    work.detect = MagicMock(side_effect=AssertionError("fresh frame must be reused"))
    work._source_entities = MagicMock(return_value={entity.name: (entity, point)})
    work.slot_bin_position = MagicMock(return_value=CONFIG["zones"]["A"]["slots"][0])
    work.move = MagicMock(side_effect=OrderError("stop at first motion"))
    admit(work, Order("a", "A", (OrderLine("corn", 1),)))
    with pytest.raises(OrderError, match="stop at first motion"):
        work.pick_and_place(Order("a", "A", (OrderLine("corn", 1),)), "fresh")
    work.detect.assert_not_called()
    work._source_entities.assert_called_once()
    assert work.physical_state_uncertain


def test_confirmed_planning_only_rejection_unlocks_candidate_without_claiming_pick():
    work = station()
    point = (0.30, 0.1, 0.03)
    entity = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    work._allocated_bin_index = 0
    work._visual_candidates = {"fresh": ("corn", point)}
    work._visual_observed_at = runtime.time.monotonic()
    work._source_entities = MagicMock(return_value={entity.name: (entity, point)})
    work.slot_bin_position = MagicMock(return_value=CONFIG["zones"]["A"]["slots"][0])
    work.move = MagicMock(side_effect=MotionPlanRejected("no safe approach"))
    admit(work, Order("a", "A", (OrderLine("corn", 1),)))
    with pytest.raises(PerceptionMismatch) as error:
        work.pick_and_place(Order("a", "A", (OrderLine("corn", 1),)), "fresh")
    assert error.value.code == "MOTION_PLAN_REJECTED"
    assert (
        not work.physical_state_uncertain and not work.inventory.has_active_execution()
    )
    assert not work._placed_entities and "fresh" not in work._visual_candidates


def test_motion_result_only_honors_safe_reselection_on_aborted_grab(monkeypatch):
    work = station()
    work._motion_client = MagicMock()
    handle = MagicMock(accepted=True)
    future = handle.get_result_async.return_value
    future.done.return_value = True
    result = SimpleNamespace(
        success=False, error="planning failed", safe_to_reselect=True
    )
    future.result.return_value = SimpleNamespace(
        status=runtime.GoalStatus.STATUS_ABORTED, result=result
    )
    monkeypatch.setattr(runtime, "get_future_result", lambda *args, **kwargs: handle)
    with pytest.raises(MotionPlanRejected):
        work.move((0.3, 0.1, 0.03), step="grab")
    with pytest.raises(OrderError) as failure:
        work.move((0.3, 0.1, 0.03), step="place")
    assert not isinstance(failure.value, MotionPlanRejected)


def test_observation_posture_requires_empty_verified_arm_and_keeps_progress(
    monkeypatch,
):
    work = station()
    work._needs_observation_motion = True
    work.physical_state_uncertain = True
    with pytest.raises(OrderError, match="uncertain"):
        work._ensure_observation_posture()
    work.connector.node.create_client.assert_not_called()
    work.physical_state_uncertain = False
    complete(
        work, Order("observed", "A", (OrderLine("corn", 2),)), "corn", "observed-corn"
    )
    work.connector.node.create_client.return_value.wait_for_service.return_value = True
    monkeypatch.setattr(
        runtime,
        "get_future_result",
        lambda *args, **kwargs: SimpleNamespace(success=True),
    )
    work._ensure_observation_posture()
    assert not work._needs_observation_motion and not work.physical_state_uncertain
    assert work._completed_by_sku == {"corn": 1} and len(work._placed_entities) == 1


def test_inventory_scan_motion_failure_requires_recovery_not_visual_retry():
    from produce_order_workstation.order_server import OrderActionServer

    server = SimpleNamespace(
        workstation=SimpleNamespace(
            scan_inventory=MagicMock(side_effect=OrderError("posture timeout")),
            physical_state_uncertain=True,
        ),
        config=CONFIG,
        _lock=Lock(),
        resources=make_resources(),
        get_logger=MagicMock(),
        publish_status=MagicMock(),
    )
    OrderActionServer._scan_stock(server)
    assert server._state == "RECOVERY_REQUIRED" and not server._scene_ready
    assert "不能只重试检测" in server._state_reason


def test_safe_partial_failure_preserves_progress_for_manual_review(monkeypatch):
    work = station()
    work._scene_prepared = True
    work.motion_attempted = True
    complete(
        work,
        Order("partial", "A", (OrderLine("corn", 1), OrderLine("carrot", 1))),
        "corn",
        "corn1",
    )
    work.physical_state_uncertain = False
    fake = MagicMock()
    fake.run.side_effect = OrderError("No carrot detected after 5 attempts")
    result = work.run(
        Order("partial", "A", (OrderLine("corn", 1), OrderLine("carrot", 1))),
        agent_executor=lambda _: fake.run(),
    )
    assert result["status"] == "PAUSED" and result["completed"] == 1
    assert result["error"].startswith("[REVIEW_REQUIRED]")


def test_uncertain_motion_failure_cannot_become_safe_pause(monkeypatch):
    work = station()
    work._scene_prepared = True
    complete(work, Order("uncertain", "A", (OrderLine("corn", 2),)), "corn", "corn1")
    work.physical_state_uncertain = True
    fake = MagicMock()
    fake.run.side_effect = OrderError("motion timeout")
    assert (
        work.run(
            Order("uncertain", "A", (OrderLine("corn", 2),)),
            agent_executor=lambda _: fake.run(),
        )["status"]
        == "FAILED"
    )


def test_review_pause_never_automatically_restarts():
    queue = make_resources()
    job = queue.submit(Order("a", "A", (OrderLine("corn", 2),)))
    with queue.store.transaction() as db:
        db.execute("UPDATE order_lines SET completed=1 WHERE order_id='a'")
    queue.finish("a", "PAUSED", 1, "[REVIEW_REQUIRED] detection exhausted")
    assert queue.get("a").review_required and queue.pending() == []
    assert queue.snapshot_orders()[0]["review_required"]


def test_fulfillment_sku_catalog_is_not_tied_to_number_of_bins():
    config = deepcopy(CONFIG)
    config["aliases"]["pear"] = ["pear"]
    config["stock"]["initial_counts"]["pear"] = 0
    validate_order(Order("new-sku", "A", (OrderLine("pear", 1),)), config)
