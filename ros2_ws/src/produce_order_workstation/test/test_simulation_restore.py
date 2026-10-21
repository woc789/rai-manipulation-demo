"""Verified checkpoint reconstruction with isolated SQLite and fake simulator."""

from copy import deepcopy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from produce_order_workstation.order import Order, OrderError, OrderLine
from produce_order_workstation.runtime import Workstation
from produce_order_workstation.simulation_restore import (
    build_restore_plan,
    restore_checkpoint,
    source_manifest,
    supersede_old_restore_intents,
)


PACKAGE = Path(__file__).parents[1]
SCENE = PACKAGE / "config/demo_scene.yaml"
CONFIG = yaml.safe_load((PACKAGE / "config/workstation.yaml").read_text())


@pytest.fixture
def checkpoint(isolated_business_database):
    work = Workstation(MagicMock(), deepcopy(CONFIG), store=isolated_business_database)
    order = Order("partial", "A", (OrderLine("corn", 2),))
    work.resources.submit(order)
    item, _ = work.inventory.observe(
        "corn", "panda_link0", [(0.56, 0.05, 0.035), (0.45, -0.09, 0.035)]
    )
    work.resources.select_next(work.inventory)
    work.inventory.begin_execution(item, order.order_id)
    work.inventory.transition(item, order.order_id, "CARRYING")
    operation = work.store.begin_operation(order.order_id, "PICK_AND_PLACE")
    work.inventory.record_placement(
        order,
        item,
        "stock_corn_1",
        "A1",
        (0.56, 0.05, 0.035),
        (0.47, -0.52),
        operation,
        "visual-only",
    )
    work.resources.recover()
    return work


def test_plan_keeps_original_id_progress_and_subtracts_confirmed_stock(checkpoint):
    plan = build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))
    assert [job.order.order_id for job in plan.jobs] == ["partial"]
    assert plan.jobs[0].completed == 1
    assert len(plan.sources) == 15 and len(plan.goods) == 1 and len(plan.bins) == 1
    assert "stock_corn_1" not in {entity.name for entity in plan.sources}
    assert plan.goods[0][0].name == "stock_corn_1"
    assert plan.goods[0][0].pose.header.frame_id == "odom"
    assert checkpoint.resources.get("partial").state == "RECOVERY_REQUIRED"


@pytest.mark.parametrize("state", ["INTENT", "UNCERTAIN"])
def test_unknown_physical_result_blocks_before_any_spawn(checkpoint, state):
    operation = checkpoint.store.begin_operation("partial", "PICK_AND_PLACE")
    if state == "UNCERTAIN":
        checkpoint.store.finish_operation(operation, state)
    with pytest.raises(OrderError, match="Unconfirmed physical"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def test_unconfirmed_maintenance_blocks_restore(checkpoint):
    with checkpoint.store.transaction() as db:
        db.execute(
            "INSERT INTO requests VALUES(?,?,?)",
            ("maintenance", json.dumps({"execution_state": "UNKNOWN"}), "APPROVED"),
        )
    with pytest.raises(OrderError, match="Unconfirmed maintenance"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def test_bad_saved_target_blocks_before_spawning(checkpoint):
    with checkpoint.store.transaction() as db:
        db.execute(
            "UPDATE placed_items SET target='[999,999]' WHERE order_id='partial'"
        )
    with pytest.raises(OrderError, match="outside"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def test_unsupported_old_manifest_fails_closed(checkpoint):
    checkpoint.config["simulation_restore"]["allow_legacy_template_checkpoint"] = False
    with pytest.raises(OrderError, match="manifest"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def test_manifest_must_match_stock_generation(checkpoint):
    checkpoint.store.set_meta(
        "simulation_source_manifest",
        {
            "epoch": checkpoint.inventory.epoch + 1,
            "entities": source_manifest(
                checkpoint.build_scene(SCENE), checkpoint.config
            ),
        },
    )
    with pytest.raises(OrderError, match="generation"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def fake_simulator(work):
    bridge = MagicMock()
    bridge.spawned_entities = []
    bridge.setup_scene.side_effect = lambda scene: bridge.spawned_entities.extend(
        scene.entities
    )
    bridge._spawn_entity.side_effect = lambda entity: bridge.spawned_entities.append(
        entity
    )
    work._scene_bridge = bridge
    work.synchronize_scene = MagicMock(return_value=["table", "bin"])
    work.entity_position = MagicMock(return_value=(0.47, -0.52, 0.09))
    return bridge


def test_restore_reuses_progress_without_new_pick_or_available_oracle(
    checkpoint, monkeypatch
):
    monkeypatch.setattr(
        "produce_order_workstation.simulation_restore.time.sleep", lambda _: None
    )
    bridge = fake_simulator(checkpoint)
    before = checkpoint.store.rows("SELECT * FROM placed_items")
    restore_checkpoint(checkpoint, SCENE)
    assert checkpoint.resources.get("partial").completed == 1
    assert checkpoint.resources.get("partial").state == "PAUSED"
    assert not checkpoint.resources.get("partial").review_required
    assert checkpoint.resources.allocation("partial").state == "PAUSED"
    assert [
        tuple(row) for row in checkpoint.store.rows("SELECT * FROM placed_items")
    ] == [tuple(row) for row in before]
    assert (
        len(bridge.spawned_entities) == 17
    )  # 15 source + 1 bin + 1 already-placed good
    assert not checkpoint.store.rows(
        "SELECT 1 FROM inventory_items WHERE state IN ('AVAILABLE','RESERVED')"
    )
    assert (
        checkpoint.store.rows(
            "SELECT count(*) FROM operations WHERE phase='PICK_AND_PLACE'"
        )[0][0]
        == 1
    )
    assert checkpoint._scene_prepared and not checkpoint.physical_state_uncertain


def test_failed_restoration_keeps_resources_quarantined_and_is_not_replayed(
    checkpoint, monkeypatch
):
    monkeypatch.setattr(
        "produce_order_workstation.simulation_restore.time.sleep", lambda _: None
    )
    fake_simulator(checkpoint)
    checkpoint.entity_position.return_value = (9.0, 9.0, 0.09)
    with pytest.raises(OrderError, match="not settled"):
        restore_checkpoint(checkpoint, SCENE)
    assert checkpoint.resources.get("partial").state == "RECOVERY_REQUIRED"
    assert checkpoint.resources.get("partial").completed == 1
    assert (
        checkpoint.store.rows(
            "SELECT state FROM operations WHERE phase='SIMULATION_RESTORE'"
        )[0][0]
        == "UNCERTAIN"
    )
    with pytest.raises(OrderError, match="Unconfirmed physical"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def test_existing_simulator_entities_cannot_be_duplicated(checkpoint):
    bridge = fake_simulator(checkpoint)
    bridge.spawned_entities = [SimpleNamespace(name="existing")]
    with pytest.raises(OrderError, match="not empty"):
        restore_checkpoint(checkpoint, SCENE)
    bridge.setup_scene.assert_not_called()


def test_collected_goods_are_not_respawned_as_source_stock(checkpoint):
    old = Order("collected", "A", (OrderLine("apple", 1),))
    checkpoint.resources.submit(old)
    item = checkpoint.inventory.observe("apple", "panda_link0", [(0.40, 0.23, 0.04)])[0]
    checkpoint.resources.allocate(old)
    checkpoint.inventory.reserve(item, old.order_id)
    checkpoint.inventory.begin_execution(item, old.order_id)
    checkpoint.inventory.transition(item, old.order_id, "CARRYING")
    operation = checkpoint.store.begin_operation(old.order_id, "PICK_AND_PLACE")
    checkpoint.inventory.record_placement(
        old,
        item,
        "stock_apple_1",
        "A2",
        (0.40, 0.23, 0.04),
        (0.57, -0.23),
        operation,
        "old-visual",
    )
    checkpoint.resources.finish_allocation(old.order_id, "COMPLETED", False)
    checkpoint.resources.finish(old.order_id, "COMPLETED", 1, "done")
    checkpoint.resources.confirm_pickup(old.order_id, checkpoint.inventory)
    plan = build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))
    assert len(plan.sources) == 14
    assert "stock_apple_1" not in {entity.name for entity in plan.sources}
    assert len(plan.goods) == 1 and len(plan.bins) == 1


def test_complete_checkpoint_returns_to_pickup_not_execution(checkpoint, monkeypatch):
    monkeypatch.setattr(
        "produce_order_workstation.simulation_restore.time.sleep", lambda _: None
    )
    with checkpoint.store.transaction() as db:
        db.execute("UPDATE order_lines SET quantity=1 WHERE order_id='partial'")
    fake_simulator(checkpoint)
    restore_checkpoint(checkpoint, SCENE)
    assert checkpoint.resources.get("partial").state == "WAITING_PICKUP"
    assert checkpoint.resources.allocation("partial").state == "WAITING_PICKUP"
    assert not checkpoint.resources.pending()


def test_new_game_discards_only_old_spawn_attempt_not_uncertain_grasp(checkpoint):
    restore = checkpoint.store.begin_operation("__workcell__", "SIMULATION_RESTORE")
    grasp = checkpoint.store.begin_operation("partial", "PICK_AND_PLACE")
    checkpoint.store.finish_operation(restore, "UNCERTAIN", {"error": "spawn failed"})
    checkpoint.store.finish_operation(grasp, "UNCERTAIN")
    supersede_old_restore_intents(checkpoint.store, "new-game", time.time())
    assert checkpoint.store.operation(restore)["state"] == "SUPERSEDED"
    assert checkpoint.store.operation(grasp)["state"] == "UNCERTAIN"
    with pytest.raises(OrderError, match="Unconfirmed physical"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))


def test_same_game_never_replays_restore(checkpoint):
    checkpoint.store.set_meta("simulation_restore_instance", "same-game")
    with pytest.raises(OrderError, match="already attempted"):
        supersede_old_restore_intents(checkpoint.store, "same-game", time.time())


def test_current_game_restore_intent_cannot_be_superseded(checkpoint):
    started = time.time() - 1
    restore = checkpoint.store.begin_operation("__workcell__", "SIMULATION_RESTORE")
    with pytest.raises(OrderError, match="still-running"):
        supersede_old_restore_intents(checkpoint.store, "current-game", started)
    assert checkpoint.store.operation(restore)["state"] == "INTENT"


def test_restage_never_places_remaining_stock_on_an_occupied_position(checkpoint):
    scene = checkpoint.build_scene(SCENE)
    originals = {entity.name: entity for entity in scene.entities}
    plan = build_restore_plan(checkpoint, scene)
    apple = next(entity for entity in plan.sources if entity.name == "stock_apple_1")
    # apple_4 still occupies the requested destination in this checkpoint.
    assert apple.pose.pose.position == originals[apple.name].pose.pose.position
    assert len({entity.name for entity in plan.sources}) == 15


def test_restage_rejects_out_of_workspace_destination(checkpoint):
    checkpoint.config["simulation_restore"]["source_position_overrides"] = {
        "stock_apple_1": [999.0, 0.0, 0.05]
    }
    with pytest.raises(OrderError, match="bounds"):
        build_restore_plan(checkpoint, checkpoint.build_scene(SCENE))
