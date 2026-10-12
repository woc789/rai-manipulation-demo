"""Regression cases for candidate handling, with no live robot or API calls."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from threading import Lock

import pytest
import yaml

from produce_order_workstation import runtime
from produce_order_workstation.inventory import InventoryLedger
from produce_order_workstation.order import Order, OrderError, OrderLine, validate_order, MotionPlanRejected, PerceptionMismatch
from produce_order_workstation.runtime import Workstation
from produce_order_workstation.scheduling import OrderQueue

CONFIG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config/workstation.yaml").read_text())


def station():
    return Workstation(MagicMock(), deepcopy(CONFIG))


def test_workstation_passes_association_gate_to_inventory():
    assert station().inventory.radius == CONFIG["perception"]["association_radius_m"]


def test_visible_alternative_exchanges_reservation_without_overpromising():
    book = InventoryLedger()
    first, alternative = book.observe("corn", "base", [(.3, -.3, .03), (.3, .3, .03)])
    order = Order("owner", "A", (OrderLine("corn", 1),))
    book.reserve(first, order.order_id)
    book.select_for_order(alternative, order)
    items = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert items[first]["state"] == "AVAILABLE" and not items[first]["order_id"]
    assert items[alternative]["order_id"] == "owner"
    assert sum(item["order_id"] == "owner" for item in items.values()) == 1


def test_alternative_cannot_steal_or_replace_unknown_physical_state():
    book = InventoryLedger()
    first, alternative = book.observe("corn", "base", [(.3, -.3, .03), (.3, .3, .03)])
    order = Order("owner", "A", (OrderLine("corn", 1),))
    book.reserve(first, "other")
    with pytest.raises(ValueError):
        book.select_for_order(first, order)
    book.release_order("other")
    book.reserve(first, "owner")
    book.observe("corn", "base", [])
    book.observe("corn", "base", [(.3, .3, .03)])
    with pytest.raises(ValueError, match="untouched"):
        book.select_for_order(alternative, order)


def test_refresh_obeys_configured_association_radius():
    book = InventoryLedger(association_radius=.02)
    item = book.observe("corn", "base", [(.3, .0, .03)])[0]
    with pytest.raises(ValueError, match="association"):
        book.reserve(item, "owner", refreshed_position=(.33, .0, .03))


def test_cross_sku_depth_jitter_cannot_double_promise_same_surface_location():
    book = InventoryLedger(association_radius=.065)
    book.observe("apple", "base", [(.3, .0, .03)])
    book.observe("tomato", "base", [(.3, .0, .10)])
    ids, missing = book.reserve_order(Order("a", "A", (OrderLine("apple", 1), OrderLine("tomato", 1))))
    assert not ids and missing == {"tomato": 1}


def test_active_grasp_does_not_expire_and_failure_releases_execution_lease():
    tick = [1.]
    book = InventoryLedger(ttl=2, clock=lambda: tick[0])
    item = book.observe("corn", "base", [(.3, .0, .03)])[0]
    book.reserve(item, "owner")
    book.begin_execution(item, "owner")
    book.release_order("owner")
    with pytest.raises(ValueError, match="in flight"):
        book.select_for_order(item, Order("owner", "A", (OrderLine("corn", 1),)))
    tick[0] = 10
    assert book.snapshot()["items"][0]["state"] == "RESERVED"
    book.transition(item, "owner", "CARRYING")
    book.finish_failed("owner", True)
    assert not book._executing
    assert book.snapshot()["items"][0]["state"] == "UNKNOWN"


def test_surface_height_shift_preserves_reserved_xy_identity():
    book = InventoryLedger(association_radius=.065)
    item = book.observe("corn", "base", [(.3, .1, .02)])[0]
    book.reserve(item, "owner")
    assert book.observe("corn", "base", [(.35, .1, .065)]) == [item]
    assert book.selectable(item, "owner") and not book.selectable(item, "other")


def test_lost_reserved_item_does_not_quarantine_unrelated_stock():
    book = InventoryLedger(association_radius=.065)
    old = book.observe("corn", "base", [(.3, -.3, .03)])[0]
    book.reserve(old, "owner")
    new = book.observe("corn", "base", [(.3, .3, .03)])[0]
    items = {item["item_id"]: item for item in book.snapshot()["items"]}
    assert items[old]["state"] == "UNKNOWN" and items[old]["order_id"] == "owner"
    assert items[new]["state"] == "AVAILABLE"


def test_local_ambiguous_replacement_stays_unavailable():
    book = InventoryLedger(association_radius=.065)
    old = book.observe("corn", "base", [(.3, .0, .03)])[0]
    book.reserve(old, "owner")
    new = book.observe("corn", "base", [(.38, .0, .03)])[0]
    assert not book.selectable(new, "other")


def test_rejection_cooldown_does_not_blacklist_neighbor_and_expires(monkeypatch):
    tick = [100.]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: tick[0])
    work = station()
    work._visual_candidates = {"bad": ("corn", (.3, .1, .03))}
    work._reject_visual_candidate(Order("a", "A", (OrderLine("corn", 1),)),
                                  "bad", "corn", (.3, .1, .03), "CLASS_CONFLICT", {})
    assert work._recently_rejected("corn", (.3, .1, .03))
    assert not work._recently_rejected("corn", (.33, .1, .03))
    tick[0] += 21
    assert not work._recently_rejected("corn", (.3, .1, .03))


def test_rejected_alias_does_not_stop_fallback():
    work = station()
    work._position_tool = MagicMock()
    work._position_tool.invoke.side_effect = [
        "Centroid(x=0.3, y=0.1, z=0.03)", "Centroid(x=0.4, y=0.1, z=0.03)"]
    work._record_perception_pipeline = MagicMock()
    work._candidate_rejections = {"bad": dict(sku="corn", position=(.3, .1, .03),
        time=runtime.time.monotonic(), code="CLASS_CONFLICT")}
    assert work.detect("corn") == [(0.4, 0.1, 0.03)]
    assert work._position_tool.invoke.call_count == 2


def test_absent_box_does_not_exclude_source_but_present_box_does():
    work = station()
    work._scene_prepared = True
    work._scene_bridge = SimpleNamespace(spawned_entities=[])
    zone = CONFIG["zones"]["A"]
    point = (*zone["slots"][0], .03)
    assert work._choose_source([point], zone) == point
    work._scene_bridge.spawned_entities = [SimpleNamespace(name="order_bin_A1")]
    with pytest.raises(OrderError):
        work._choose_source([point], zone)


def test_fresh_visual_target_skips_redundant_inference_without_skipping_interlock():
    work = station()
    point = (.30, .1, .03)
    entity = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    work._allocated_bin_index = 0
    work._visual_candidates = {"fresh": ("corn", point)}
    work._visual_observed_at = runtime.time.monotonic()
    work.detect = MagicMock(side_effect=AssertionError("fresh frame must be reused"))
    work._source_entities = MagicMock(return_value={entity.name: (entity, point)})
    work.slot_bin_position = MagicMock(return_value=CONFIG["zones"]["A"]["slots"][0])
    work.move = MagicMock(side_effect=OrderError("stop at first motion"))
    with pytest.raises(OrderError, match="stop at first motion"):
        work.pick_and_place(Order("a", "A", (OrderLine("corn", 1),)), "fresh")
    work.detect.assert_not_called()
    work._source_entities.assert_called_once()
    assert work.physical_state_uncertain


def test_confirmed_planning_only_rejection_unlocks_candidate_without_claiming_pick():
    work = station()
    point = (.30, .1, .03)
    entity = SimpleNamespace(name="stock_corn_1", prefab_name="corn")
    work._allocated_bin_index = 0
    work._visual_candidates = {"fresh": ("corn", point)}
    work._visual_observed_at = runtime.time.monotonic()
    work._source_entities = MagicMock(return_value={entity.name: (entity, point)})
    work.slot_bin_position = MagicMock(return_value=CONFIG["zones"]["A"]["slots"][0])
    work.move = MagicMock(side_effect=MotionPlanRejected("no safe approach"))
    with pytest.raises(PerceptionMismatch) as error:
        work.pick_and_place(Order("a", "A", (OrderLine("corn", 1),)), "fresh")
    assert error.value.code == "MOTION_PLAN_REJECTED"
    assert not work.physical_state_uncertain and not work.inventory._executing
    assert not work._placed_entities and "fresh" not in work._visual_candidates


def test_motion_result_only_honors_safe_reselection_on_aborted_grab(monkeypatch):
    work = station()
    work._motion_client = MagicMock()
    handle = MagicMock(accepted=True)
    future = handle.get_result_async.return_value
    future.done.return_value = True
    result = SimpleNamespace(success=False, error="planning failed", safe_to_reselect=True)
    future.result.return_value = SimpleNamespace(status=runtime.GoalStatus.STATUS_ABORTED, result=result)
    monkeypatch.setattr(runtime, "get_future_result", lambda *args, **kwargs: handle)
    with pytest.raises(MotionPlanRejected):
        work.move((.3, .1, .03), step="grab")
    with pytest.raises(OrderError) as failure:
        work.move((.3, .1, .03), step="place")
    assert not isinstance(failure.value, MotionPlanRejected)


def test_observation_posture_requires_empty_verified_arm_and_keeps_progress(monkeypatch):
    work = station()
    work._needs_observation_motion = True
    work.physical_state_uncertain = True
    with pytest.raises(OrderError, match="uncertain"):
        work._ensure_observation_posture()
    work.connector.node.create_client.assert_not_called()
    work.physical_state_uncertain = False
    work._completed_by_sku = {"corn": 1}
    work._placed_entities = [("corn", None, 0, (.3, .1, .03))]
    work.connector.node.create_client.return_value.wait_for_service.return_value = True
    monkeypatch.setattr(runtime, "get_future_result", lambda *args, **kwargs: SimpleNamespace(success=True))
    work._ensure_observation_posture()
    assert not work._needs_observation_motion and not work.physical_state_uncertain
    assert work._completed_by_sku == {"corn": 1} and len(work._placed_entities) == 1


def test_inventory_scan_motion_failure_requires_recovery_not_visual_retry():
    from produce_order_workstation.order_server import OrderActionServer
    server = SimpleNamespace(
        workstation=SimpleNamespace(scan_inventory=MagicMock(side_effect=OrderError("posture timeout")),
                                    physical_state_uncertain=True),
        config=CONFIG, _lock=Lock(), queue=OrderQueue(),
        get_logger=MagicMock(), publish_status=MagicMock())
    OrderActionServer._scan_stock(server)
    assert server._state == "RECOVERY_REQUIRED" and not server._scene_ready
    assert "不能只重试检测" in server._state_reason


def test_safe_partial_failure_preserves_progress_for_manual_review(monkeypatch):
    work = station()
    work._scene_prepared = True
    work.motion_attempted = True
    work._placed_entities = [("corn", SimpleNamespace(name="corn1"), 0, (.3, .1, .03))]
    work._completed_by_sku = {"corn": 1}
    work.physical_state_uncertain = False
    fake = MagicMock()
    fake.run.side_effect = OrderError("No carrot detected after 5 attempts")
    result = work.run(Order("partial", "A", (OrderLine("corn", 1), OrderLine("carrot", 1))),
                      agent_executor=lambda _: fake.run())
    assert result["status"] == "PAUSED" and result["completed"] == 1
    assert result["error"].startswith("[REVIEW_REQUIRED]")


def test_uncertain_motion_failure_cannot_become_safe_pause(monkeypatch):
    work = station()
    work._scene_prepared = True
    work._placed_entities = [("corn", SimpleNamespace(name="corn1"), 0, (.3, .1, .03))]
    work.physical_state_uncertain = True
    fake = MagicMock()
    fake.run.side_effect = OrderError("motion timeout")
    assert work.run(Order("uncertain", "A", (OrderLine("corn", 2),)),
                    agent_executor=lambda _: fake.run())["status"] == "FAILED"


def test_review_pause_never_automatically_restarts():
    queue = OrderQueue()
    job = queue.submit(Order("a", "A", (OrderLine("corn", 2),)))
    queue.finish("a", "PAUSED", 1, "[REVIEW_REQUIRED] detection exhausted")
    assert job.review_required and queue.pending() == []
    assert queue.snapshot()[0]["review_required"]


def test_fulfillment_sku_catalog_is_not_tied_to_number_of_bins():
    config = deepcopy(CONFIG)
    config["aliases"]["pear"] = ["pear"]
    config["stock"]["initial_counts"]["pear"] = 0
    validate_order(Order("new-sku", "A", (OrderLine("pear", 1),)), config)
