from copy import deepcopy
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml
from rclpy.action import GoalResponse

from produce_order_interfaces.action import ExecuteOrder
from produce_order_interfaces.msg import OrderLine as RosOrderLine
from produce_order_interfaces.srv import ConfirmPickup
from db_support import make_resources, complete
from produce_order_workstation.order import (
    Order,
    OrderError,
    OrderLine,
    plan_placements,
    validate_order,
)
from produce_order_workstation.order_server import OrderActionServer
from db_support import make_inventory
from produce_order_workstation.runtime import Workstation


PACKAGE = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


def order(name="mixed"):
    return Order(
        name,
        "A",
        (
            OrderLine("corn", 1),
            OrderLine("carrot", 1),
            OrderLine("apple", 1),
            OrderLine("tomato", 1),
        ),
    )


def server_stub(manager):
    station = MagicMock()
    station.inventory = make_inventory(store=manager.store)
    station.physical_state_uncertain = False
    for sku, x in (("corn", 0.3), ("carrot", 0.4), ("apple", 0.5), ("tomato", 0.6)):
        station.inventory.observe(sku, "base", [(x, -0.1, 0.04), (x, 0.1, 0.04)])
    return SimpleNamespace(
        _lock=Lock(),
        _busy=False,
        _scene_ready=True,
        _state="READY",
        _state_reason="",
        config=CONFIG,
        resources=manager,
        store=manager.store,
        _dispatch_order_id=None,
        _dispatch_timed_out=False,
        publish_status=MagicMock(),
        get_logger=MagicMock(),
        workstation=station,
        report_path=lambda _: Path("/nonexistent-report"),
    )


def test_fulfillment_is_the_default_and_mixed_order_uses_one_bin():
    assert CONFIG["operation_mode"] == "fulfillment"
    placements = plan_placements(order(), CONFIG, bin_index=2)
    assert [p.bin_index for p in placements] == [2] * 4
    assert len({(p.offset_x, p.offset_y) for p in placements}) == 4
    assert [p.sku for p in placements] == ["corn", "carrot", "apple", "tomato"]


def test_offsets_are_global_not_restarted_for_each_sku():
    placements = plan_placements(
        Order("two", "A", (OrderLine("corn", 1), OrderLine("apple", 1))), CONFIG
    )
    assert len({(p.offset_x, p.offset_y) for p in placements}) == 2


def test_total_capacity_applies_to_whole_order():
    with pytest.raises(ValueError, match="at most 4"):
        validate_order(
            Order("large", "A", (OrderLine("corn", 3), OrderLine("apple", 2))), CONFIG
        )
    modified = deepcopy(CONFIG)
    modified["operation_mode"] = "classification"
    validate_order(
        Order("legacy", "A", (OrderLine("corn", 3), OrderLine("apple", 2))), modified
    )


@pytest.mark.parametrize("index", [-1, 4, True])
def test_invalid_bin_is_rejected(index):
    with pytest.raises(ValueError, match="allocated bin"):
        plan_placements(order(), CONFIG, bin_index=index)


def test_completed_order_keeps_ownership_and_next_order_uses_next_bin():
    manager = make_resources(CONFIG)
    assert manager.allocate(order("first")).bin_indices == (0,)
    manager.finish_allocation("first", "COMPLETED", True)
    assert manager.bin("A1").state == "WAITING_PICKUP"
    assert not manager.can_reset()
    assert manager.allocate(order("second")).bin_indices == (1,)
    assert manager.pickup("first").order == order("first")
    manager.confirm_pickup("first", make_inventory(store=manager.store))
    assert manager.bin("A1").state == "FREE"
    assert manager.bin("A2").order_id == "second"
    assert manager.allocation("first").state == "COLLECTED"
    with pytest.raises(ValueError):
        manager.allocate(order("first"))


def test_full_bins_require_pickup_and_release_only_selected_order():
    manager = make_resources(CONFIG)
    for i in range(4):
        manager.allocate(order(str(i)))
        manager.finish_allocation(str(i), "COMPLETED", True)
    assert not manager.can_allocate("A")
    with pytest.raises(ValueError, match="No receiving bin"):
        manager.allocate(order("fifth"))
    manager.confirm_pickup("1", make_inventory(store=manager.store))
    assert manager.allocate(order("fifth")).bin_indices == (1,)
    assert manager.bin("A1").order_id == "0"


def test_motion_failure_keeps_bin_but_failure_before_motion_releases_it():
    manager = make_resources(CONFIG)
    manager.allocate(order("no-motion"))
    manager.finish_allocation("no-motion", "FAILED", False)
    assert manager.bin("A1").state == "FREE"
    manager.allocate(order("moved"))
    manager.finish_allocation("moved", "CANCELLED", True)
    assert manager.bin("A1").state == "RECOVERY_REQUIRED"
    with pytest.raises(ValueError, match="completed"):
        manager.confirm_pickup("moved", make_inventory(store=manager.store))
    manager.reset()
    assert manager.bin("A1").state == "FREE"
    assert manager.allocation("moved").state == "DISCARDED_BY_RESET"


def test_reset_cannot_destroy_completed_orders():
    manager = make_resources(CONFIG)
    manager.allocate(order())
    manager.finish_allocation("mixed", "COMPLETED", True)
    server = server_stub(manager)
    response = SimpleNamespace(success=False, message="")
    OrderActionServer.recover_callback(server, None, response)
    assert not response.success
    assert "先确认取走" in response.message
    server.workstation.prepare_scene.assert_not_called()


def test_pickup_service_cleanup_failure_retains_ownership_and_can_retry(tmp_path):
    manager = make_resources(CONFIG)
    manager.allocate(order())
    manager.finish_allocation("mixed", "COMPLETED", True)
    server = server_stub(manager)
    server.report_path = lambda _: tmp_path / "mixed.json"
    server.workstation.collect_order.side_effect = RuntimeError("delete timeout")
    request = ConfirmPickup.Request(order_id="mixed")
    response = OrderActionServer.pickup_callback(
        server, request, ConfirmPickup.Response()
    )
    assert not response.success
    assert manager.bin("A1").state == "WAITING_PICKUP"
    assert not server._busy
    server.workstation.collect_order.side_effect = None
    response = OrderActionServer.pickup_callback(
        server, request, ConfirmPickup.Response()
    )
    assert response.success
    assert manager.bin("A1").state == "FREE"
    assert (tmp_path / "mixed.pickup.json").is_file()
    server.workstation.prepare_scene.assert_not_called()


def test_pickup_is_rejected_while_robot_is_busy():
    server = server_stub(make_resources(CONFIG))
    server._busy = True
    response = OrderActionServer.pickup_callback(
        server, ConfirmPickup.Request(order_id="unknown"), ConfirmPickup.Response()
    )
    assert not response.success
    server.workstation.collect_order.assert_not_called()


def test_goal_allocates_bin_before_acceptance_and_busy_rejection_does_not_allocate():
    manager = make_resources(CONFIG)
    server = server_stub(manager)
    goal = ExecuteOrder.Goal(
        order_id="first",
        zone="A",
        prepare_scene=True,
        lines=[RosOrderLine(sku="corn", quantity=1)],
    )
    assert OrderActionServer.goal_callback(server, goal) == GoalResponse.ACCEPT
    assert manager.bin("A1").order_id == "first"
    goal.order_id = "second"
    assert OrderActionServer.goal_callback(server, goal) == GoalResponse.REJECT
    assert manager.allocation("second") is None


def test_legacy_classification_allocates_sku_bins():
    config = deepcopy(CONFIG)
    config["operation_mode"] = "classification"
    manager = make_resources(config)
    assert manager.allocate(order()).bin_indices == (0, 1, 2, 3)
    manager.finish_allocation("mixed", "COMPLETED", True)
    manager.confirm_pickup("mixed", make_inventory(store=manager.store))
    assert all(b["state"] == "FREE" for b in manager.snapshot_bins())


def test_complete_action_keeps_scene_and_records_allocated_bin(tmp_path):
    manager = make_resources(CONFIG)
    server = server_stub(manager)
    server.report_path = lambda order_id: tmp_path / f"{order_id}.json"
    server._publish_feedback = MagicMock()
    request = ExecuteOrder.Goal(
        order_id="done",
        zone="A",
        prepare_scene=True,
        lines=[RosOrderLine(sku="corn", quantity=1)],
    )
    assert OrderActionServer.goal_callback(server, request) == GoalResponse.ACCEPT
    server.workstation.motion_attempted = True
    server.workstation._placed_entities = [("corn", "entity", 0, (0.3, 0, 0.04))]

    def verified_run(_order):
        with server.store.transaction() as db:
            db.execute(
                "UPDATE order_lines SET legacy_completed=1 WHERE order_id='done'"
            )
        return dict(status="COMPLETED", completed=1, requested=1, error=None)

    server.workstation.run.side_effect = verified_run
    handle = MagicMock()
    handle.request = request
    response = OrderActionServer.execute_callback(server, handle)
    assert response.status == "COMPLETED"
    server.workstation.begin_order.assert_called_once_with(
        Order("done", "A", (OrderLine("corn", 1),)), bin_index=0
    )
    server.workstation.prepare_scene.assert_not_called()
    handle.succeed.assert_called_once()
    assert server._scene_ready and not server._busy
    assert manager.bin("A1").state == "WAITING_PICKUP"
    report = yaml.safe_load((tmp_path / "done.json").read_text())
    assert report["bin_ids"] == ["A1"]
    assert report["delivery_state"] == "WAITING_PICKUP"


def test_receipt_export_failure_does_not_undo_committed_pickup(tmp_path):
    manager = make_resources(CONFIG)
    manager.allocate(order())
    manager.finish_allocation("mixed", "COMPLETED", True)
    server = server_stub(manager)
    # A regular file cannot serve as the receipt directory.
    blocked = tmp_path / "blocked"
    blocked.write_text("test")
    server.report_path = lambda _: blocked / "mixed.json"
    result = OrderActionServer.pickup_callback(
        server, ConfirmPickup.Request(order_id="mixed"), ConfirmPickup.Response()
    )
    assert result.success
    assert manager.bin("A1").state == "FREE"
    assert manager.get("mixed").state == "COLLECTED"
    assert not server._busy


def test_begin_order_preserves_cross_order_delivered_names():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    complete(
        station,
        Order("previous", "A", (OrderLine("apple", 1),)),
        "apple",
        "previous_apple",
    )
    station.begin_order(order(), bin_index=2)
    assert station._allocated_bin_index == 2
    assert station._delivered_entity_names == {"previous_apple"}
    assert station._placed_entities == []


def pickup_station():
    station = Workstation(MagicMock(), CONFIG)
    station._scene_prepared = True
    own = SimpleNamespace(name="own_apple", prefab_name="apple")
    other = SimpleNamespace(name="other_corn", prefab_name="corn")
    box = SimpleNamespace(name="order_bin_A1", prefab_name="toy_box")
    table = SimpleNamespace(name="table_tomato", prefab_name="tomato")
    bridge = MagicMock()
    bridge.spawned_entities = [own, other, box, table]
    bridge._despawn_entity.side_effect = lambda entity: bridge.spawned_entities.remove(
        entity
    )
    station._scene_bridge = bridge
    station.synchronize_scene = MagicMock()
    complete(station, Order("own", "A", (OrderLine("apple", 1),)), "apple", own.name)
    complete(station, Order("other", "A", (OrderLine("corn", 1),)), "corn", other.name)
    slot = tuple(CONFIG["zones"]["A"]["slots"][0])
    station.slot_bin_position = MagicMock(return_value=slot)
    station.observe_after_place = MagicMock(return_value=[(*slot, 0.05)])
    return station, own, [("apple", own, 0, (0.30, -0.1, 0.04))]


def test_pickup_deletes_only_order_goods_and_repeat_cleanup_is_safe():
    station, own, placed = pickup_station()
    station.collect_order(order(), placed)
    station._scene_bridge._despawn_entity.assert_any_call(own)
    assert [e.name for e in station._scene_bridge.spawned_entities] == [
        "other_corn",
        "table_tomato",
    ]
    station.collect_order(order(), placed)
    assert station._scene_bridge._despawn_entity.call_count == 2  # goods + their box


def test_pickup_preflight_refuses_goods_outside_bin_before_deleting():
    station, _, placed = pickup_station()
    station.observe_after_place.return_value = [(0.30, -0.1, 0.04)]
    with pytest.raises(OrderError, match="outside"):
        station.collect_order(order(), placed)
    station._scene_bridge._despawn_entity.assert_not_called()
