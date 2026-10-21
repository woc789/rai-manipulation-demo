"""Reconstruct a simulator at a verified business checkpoint, never a real robot.

Persisted placements are completion evidence, not current perception. Rebuilt
source stock must be observed again before any motion is admitted.
"""

from collections import Counter
from dataclasses import dataclass
import json
from math import isfinite
from pathlib import Path
import time

from rai_sim.simulation_bridge import Entity, SceneConfig

from .order import OrderError, fulfillment_mode, verify_bin_placement
from .stock_layout import validate_stock_layout


@dataclass
class RestorePlan:
    sources: list
    bins: list
    goods: list
    jobs: list
    templates: dict


def source_manifest(scene, config):
    return [
        entity.model_dump(mode="json")
        for entity in scene.entities
        if entity.prefab_name in config["aliases"]
    ]


def supersede_old_restore_intents(store, instance_id, started_at):
    """A new GameLauncher discarded old spawn-only effects, not arm operations."""
    if not instance_id or not isfinite(started_at) or not 0 < started_at <= time.time():
        raise OrderError("Missing fresh simulator launch identity")
    if store.meta("simulation_restore_instance") == instance_id:
        raise OrderError("This simulator instance has already attempted reconstruction")
    with store.transaction() as db:
        for row in store.rows(
            "SELECT * FROM operations WHERE phase='SIMULATION_RESTORE' AND state IN ('INTENT','UNCERTAIN')"
        ):
            if row["updated"] >= started_at:
                raise OrderError(
                    "Restore attempt belongs to this still-running simulator"
                )
            result = json.loads(row["result"] or "{}")
            result["superseded_by_fresh_simulator"] = instance_id
            db.execute(
                "UPDATE operations SET state='SUPERSEDED',result=?,updated=? WHERE operation_id=?",
                (json.dumps(result), time.time(), row["operation_id"]),
            )


def build_restore_plan(station, scene):
    """Validate all evidence before spawning or releasing a single resource."""
    config, store, resources = station.config, station.store, station.resources
    if fulfillment_mode(config) != "fulfillment":
        raise OrderError("Simulator checkpoint restore requires fulfillment mode")
    validate_stock_layout(scene, config)
    if config["planning_scene"]["barrier"].get("enabled", True):
        raise OrderError(
            "Checkpoint reconstruction currently requires the obstacle-disabled scene"
        )
    if store.rows(
        "SELECT 1 FROM operations WHERE state IN ('INTENT','UNCERTAIN') LIMIT 1"
    ):
        raise OrderError("Unconfirmed physical operation: automatic restore forbidden")
    for row in store.rows("SELECT body FROM requests WHERE state='APPROVED'"):
        if json.loads(row["body"]).get("execution_state") in (
            "UNKNOWN",
            "ADMITTING",
            "RUNNING",
        ):
            raise OrderError("Unconfirmed maintenance: automatic restore forbidden")
    allocations = store.rows(
        "SELECT a.*,b.bin_index,b.zone FROM bin_allocations a JOIN bins b USING(bin_id) "
        "WHERE a.released IS NULL ORDER BY a.bin_id"
    )
    if not allocations:
        raise OrderError("No retained order bin to restore")
    templates = {entity.name: entity for entity in scene.entities}
    rows = store.rows(
        "SELECT p.*,i.epoch FROM placed_items p JOIN inventory_items i USING(item_id)"
    )
    manifest = store.meta("simulation_source_manifest")
    if manifest is None and not config.get("simulation_restore", {}).get(
        "allow_legacy_template_checkpoint", False
    ):
        raise OrderError(
            "No simulator source manifest; legacy reconstruction needs explicit opt-in"
        )
    sources = (
        [Entity.model_validate(row) for row in manifest["entities"]]
        if manifest
        else [
            entity.model_copy(deep=True)
            for entity in scene.entities
            if entity.prefab_name in config["aliases"]
        ]
    )
    if manifest and manifest["epoch"] != station.inventory.epoch:
        raise OrderError("Source manifest belongs to a different stock generation")
    source_by_name = {entity.name: entity for entity in sources}
    if any(entity.prefab_name not in config["aliases"] for entity in sources):
        raise OrderError("Source manifest contains non-stock prefabs")
    consumed = {
        row["entity_name"] for row in rows if row["epoch"] == station.inventory.epoch
    }
    if not consumed.issubset(source_by_name):
        raise OrderError(
            "Stock-generation templates unavailable; cannot invent remaining stock"
        )
    bins, goods, jobs = [], [], {}
    seen_goods = set()
    for allocation in allocations:
        identifier = allocation["order_id"]
        job = resources.get(identifier)
        if (
            job.state != "RECOVERY_REQUIRED"
            or allocation["state"] != "RECOVERY_REQUIRED"
        ):
            raise OrderError("Only quarantined restart orders can be reconstructed")
        zone = config["zones"][allocation["zone"]]
        index = allocation["bin_index"]
        name = zone["slot_entities"][index]
        if name not in templates:
            raise OrderError("Retained bin template is missing")
        bins.append(templates[name].model_copy(deep=True))
        jobs[identifier] = job
        for row in (
            row
            for row in rows
            if row["order_id"] == identifier and row["bin_id"] == allocation["bin_id"]
        ):
            operation = store.operation(row["operation_id"])
            if (
                not operation
                or operation["state"] != "VERIFIED"
                or operation["order_id"] != identifier
                or operation["phase"] != "PICK_AND_PLACE"
            ):
                raise OrderError("Placement has no verified physical operation")
            if row["entity_name"] in seen_goods:
                raise OrderError("Duplicate physical identity in retained boxes")
            seen_goods.add(row["entity_name"])
            base_name = row["entity_name"].split("_refill_", 1)[0]
            template = templates.get(base_name)
            if template is None or template.prefab_name != row["sku"]:
                raise OrderError("Placed-item prefab identity cannot be reconstructed")
            target = tuple(json.loads(row["target"]))
            center = tuple(zone["slots"][index])
            if (
                len(target) != 2
                or not all(isfinite(value) for value in target)
                or any(
                    abs(target[axis] - center[axis]) > zone["bin_half_extents_m"][axis]
                    for axis in (0, 1)
                )
            ):
                raise OrderError("Recorded placement lies outside its retained bin")
            entity = template.model_copy(deep=True)
            entity.name = row["entity_name"]
            # The O3DE adapter expects table-local odom poses, as in source
            # templates. Its spawn conversion cannot accept MoveIt's virtual
            # panda_link0 frame as a substitute for that simulator frame.
            if entity.pose.header.frame_id != "odom":
                raise OrderError(
                    "Checkpoint adapter requires the demo's table-local odom frame"
                )
            entity.pose.pose.position.x, entity.pose.pose.position.y = target
            entity.pose.pose.position.z = config["motion"]["bin_release_z_m"]
            goods.append((entity, dict(row), center, zone))
    for identifier, job in jobs.items():
        counts = Counter(
            row["sku"] for _, row, _, _ in goods if row["order_id"] == identifier
        )
        if sum(counts.values()) != job.completed or any(
            counts[sku] != count
            for sku, count in resources.progress(identifier).items()
        ):
            raise OrderError(
                "Progress is not fully backed by retained placement evidence"
            )
    remaining = [entity for entity in sources if entity.name not in consumed]
    overrides = config.get("simulation_restore", {}).get(
        "source_position_overrides", {}
    )
    minimum = config["stock"]["layout"]["min_center_spacing_m"]
    for entity in remaining:
        target = overrides.get(entity.name)
        if target is None:
            continue
        if len(target) != 3 or not all(isfinite(v) for v in target):
            raise OrderError("Invalid resumed stock layout")
        if any(
            (other.pose.pose.position.x - target[0]) ** 2
            + (other.pose.pose.position.y - target[1]) ** 2
            < minimum**2
            for other in remaining
            if other.name != entity.name
        ):
            continue  # Never move onto stock still present in a different checkpoint.
        x, y, z = target
        if not (
            config["workspace"]["x"][0] <= x <= config["workspace"]["x"][1]
            and config["workspace"]["y"][0] <= y <= config["workspace"]["y"][1]
            and x * x + y * y >= config["stock"]["layout"]["min_base_radius_m"] ** 2
            and 0 <= z <= 0.10
        ):
            raise OrderError("Resumed stock position violates workcell bounds")
        if any(
            (x - bx) ** 2 + (y - by) ** 2
            < config["stock"]["layout"]["min_bin_distance_m"] ** 2
            for zone in config["zones"].values()
            for bx, by in zone["slots"]
        ):
            raise OrderError("Resumed stock position overlaps a receiving bin")
        (
            entity.pose.pose.position.x,
            entity.pose.pose.position.y,
            entity.pose.pose.position.z,
        ) = target
    return RestorePlan(
        remaining,
        bins,
        goods,
        list(jobs.values()),
        templates,
    )


def restore_checkpoint(station, scene_path):
    """Spawn stock minus confirmed consumption, then validate retained box goods."""
    from rai_sim.o3de.o3de_bridge import O3DExROS2Bridge

    scene = station.build_scene(Path(scene_path))
    with station.store.read_transaction():
        plan = build_restore_plan(station, scene)
    if station._scene_bridge is not None and station._scene_bridge.spawned_entities:
        raise OrderError("Simulator is not empty; refusing duplicate reconstruction")
    with station.store.transaction():
        operation = station.store.begin_operation("__workcell__", "SIMULATION_RESTORE")
        instance_id = getattr(station, "_simulation_instance_id", None)
        if instance_id:
            station.store.set_meta("simulation_restore_instance", instance_id)
    try:
        station._scene_bridge = station._scene_bridge or O3DExROS2Bridge(
            station.connector
        )
        bridge = station._scene_bridge
        bridge.service_timeout_sec = station.config.get("scene_service_timeout_s", 20)
        station.physical_state_uncertain = True
        bridge.setup_scene(SceneConfig(entities=plan.sources + plan.bins))
        time.sleep(station.config["stock"]["settle_time_s"])
        for entity, _, _, _ in plan.goods:
            bridge._spawn_entity(entity)
            time.sleep(station.config["motion"]["settle_time_s"])
        tracked = {entity.name: entity for entity in bridge.spawned_entities}
        for entity, row, center, zone in plan.goods:
            position = station.entity_position(tracked[entity.name])
            if (
                not all(isfinite(v) for v in position)
                or not -0.02 <= position[2] <= 0.30
                or not verify_bin_placement(
                    [position],
                    tuple(json.loads(row["source"])),
                    center,
                    tuple(zone["bin_half_extents_m"]),
                    station.config["workspace"]["source_exclusion_radius_m"],
                )
            ):
                raise OrderError(
                    f"Reconstructed good {entity.name} is not settled inside its bin: {position}"
                )
        station._bin_templates = {
            name: plan.templates[name]
            for zone in station.config["zones"].values()
            for name in zone["slot_entities"]
        }
        station.synchronize_scene()
        with station.store.transaction() as db:
            # Never seed AVAILABLE inventory from simulator/template positions.
            db.execute(
                "UPDATE reservations SET released=?,updated=? WHERE released IS NULL "
                "AND item_id IN (SELECT item_id FROM inventory_items WHERE state NOT IN ('PLACED','COLLECTED'))",
                (time.time(), time.time()),
            )
            db.execute(
                "UPDATE inventory_items SET state='RETIRED',executing=0 "
                "WHERE state NOT IN ('PLACED','COLLECTED')"
            )
            if station.store.meta("simulation_source_manifest") is None:
                station.store.set_meta(
                    "simulation_source_manifest",
                    {
                        "epoch": station.inventory.epoch,
                        "entities": source_manifest(scene, station.config),
                    },
                )
            for job in plan.jobs:
                complete = job.completed == job.total
                station.resources.allocation_state(
                    job.order.order_id, "WAITING_PICKUP" if complete else "PAUSED"
                )
                station.resources.update(
                    job.order.order_id,
                    expected_version=job.version,
                    state="WAITING_PICKUP" if complete else "PAUSED",
                    review_required=False,
                    reason="仿真检查点已恢复；重新视觉定位后续做剩余任务"
                    if not complete
                    else "仿真订单箱已恢复，等待取货",
                )
            station.store.finish_operation(
                operation,
                "VERIFIED",
                {
                    "orders": [job.order.order_id for job in plan.jobs],
                    "restored_goods": len(plan.goods),
                    "source_count": len(plan.sources),
                },
            )
        station._scene_prepared = True
        station.physical_state_uncertain = False
        station._needs_observation_motion = True
        station.event(
            "",
            "SIMULATION_CHECKPOINT_RESTORED",
            orders=[job.order.order_id for job in plan.jobs],
            restored_goods=len(plan.goods),
            source_count=len(plan.sources),
        )
        return plan
    except Exception as exc:
        station._scene_prepared = False
        station.physical_state_uncertain = True
        if station.store.operation(operation)["state"] == "INTENT":
            station.store.finish_operation(operation, "UNCERTAIN", {"error": str(exc)})
        raise
