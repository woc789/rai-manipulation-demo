"""Synthetic database setup for domain tests; never points at the operator's file."""

from contextvars import ContextVar
from pathlib import Path
import yaml

from produce_order_workstation.inventory_repository import InventoryRepository
from produce_order_workstation.resource_repository import ResourceRepository
from produce_order_workstation.order import Order, OrderLine

CURRENT = ContextVar("test_business_database")
CONFIG = yaml.safe_load(
    (Path(__file__).parents[1] / "config/workstation.yaml").read_text()
)


def make_inventory(*args, store=None, **kwargs):
    return InventoryRepository(store or CURRENT.get(), *args, **kwargs)


def make_resources(config=None, *, max_pending=100, store=None):
    return ResourceRepository(
        store or CURRENT.get(), config or CONFIG, max_pending=max_pending
    )


def register(book, order):
    resources = make_resources(store=book.store)
    if resources.get(order.order_id) is None:
        resources.submit(order)
    return resources


def reserve(book, item, owner, **kwargs):
    sku = next(row["sku"] for row in book.snapshot()["items"] if row["item_id"] == item)
    register(book, Order(owner, "A", (OrderLine(sku, 100),)))
    return book.reserve(item, owner, **kwargs)


def reserve_order(book, order, **kwargs):
    register(book, order)
    return book.reserve_order(order, **kwargs)


def mark_placed(book, item, owner):
    resources = make_resources(store=book.store)
    order = resources.get(owner).order
    allocation = resources.allocate(order)
    row = next(row for row in book.snapshot()["items"] if row["item_id"] == item)
    operation = book.store.begin_operation(owner, "PICK_AND_PLACE")
    book.record_placement(
        order,
        item,
        "fake-" + item,
        f"A{allocation.bin_indices[0] + 1}",
        row["position"],
        (0.5, -0.48, 0.1),
        operation,
        "fixture",
    )


def seed_progress(station, counts, identifier=None):
    """Fixture-only evidence; tests of real placement use record_placement instead."""
    identifier = (
        identifier or getattr(station, "_active_order_id", None) or "fixture-order"
    )
    resources = station.resources
    job = resources.get(identifier)
    if job is None:
        resources.submit(
            Order(
                identifier,
                "A",
                tuple(OrderLine(sku, max(4, count)) for sku, count in counts.items()),
            )
        )
    with station.store.transaction() as db:
        for sku, count in counts.items():
            if not db.execute(
                "SELECT 1 FROM order_lines WHERE order_id=? AND sku=?",
                (identifier, sku),
            ).fetchone():
                db.execute(
                    "INSERT INTO order_lines VALUES(?,?,?,?)",
                    (identifier, sku, max(4, count), count),
                )
            else:
                db.execute(
                    "UPDATE order_lines SET completed=? WHERE order_id=? AND sku=?",
                    (count, identifier, sku),
                )
    station._active_order_id = identifier


def admit(station, order):
    if station.resources.get(order.order_id) is None:
        station.resources.submit(order)
    station.resources.allocate(order)
    station._active_order_id = order.order_id


def complete(station, order, sku, entity_name=None):
    """A fake mechanical skill with real verified SQLite transitions."""
    admit(station, order)
    done = station.resources.progress(order.order_id).get(sku, 0)
    point = (0.3 + done * 0.12, 0.1, 0.04)
    item = station.inventory.observe(sku, "test", [point], complete_scan=False)[0]
    station.inventory.reserve(item, order.order_id)
    station.inventory.transition(item, order.order_id, "CARRYING")
    allocation = station.resources.allocation(order.order_id)
    if station.config.get("operation_mode") == "classification":
        index = station.config["zones"][order.zone]["sku_bins"][sku]
    else:
        index = allocation.bin_indices[0]
    operation = getattr(
        station, "_operation_id", None
    ) or station.store.begin_operation(order.order_id, "PICK_AND_PLACE")
    station.inventory.record_placement(
        order,
        item,
        entity_name or "fake-" + item,
        f"{order.zone}{index + 1}",
        point,
        (*station.config["zones"][order.zone]["slots"][index], 0.1),
        operation,
        "fixture",
    )
