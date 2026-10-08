"""In-memory bin ownership; the ROS server serializes all mutations.

This is not the future inventory ledger or durable recovery database.
"""

from dataclasses import dataclass, field

from .order import Order, fulfillment_mode


@dataclass
class BinResource:
    bin_id: str
    zone: str
    index: int
    state: str = "FREE"
    order_id: str | None = None


@dataclass
class OrderAllocation:
    order: Order
    bin_indices: tuple[int, ...]
    state: str = "ALLOCATED"
    entities: list = field(default_factory=list)


class BinManager:
    """Own bin lifecycle, not robot execution or perception decisions."""

    def __init__(self, config: dict):
        self.config = config
        self.bins = {
            f"{zone}{index + 1}": BinResource(f"{zone}{index + 1}", zone, index)
            for zone, data in config["zones"].items()
            for index in range(len(data["slots"]))
        }
        self.orders: dict[str, OrderAllocation] = {}

    def can_allocate(self, zone: str) -> bool:
        available = [b for b in self.bins.values() if b.zone == zone]
        if fulfillment_mode(self.config) == "fulfillment":
            return any(b.state == "FREE" for b in available)
        # Conservative legacy mode: do not overlap classification batches.
        return all(b.state == "FREE" for b in available)

    def allocate(self, order: Order) -> OrderAllocation:
        if order.order_id in self.orders:
            raise ValueError("Order ID already owns a bin or was collected")
        if not self.can_allocate(order.zone):
            raise ValueError("No receiving bin is available; confirm pickup first")
        if fulfillment_mode(self.config) == "fulfillment":
            indices = (next(b.index for b in self.bins.values()
                            if b.zone == order.zone and b.state == "FREE"),)
        else:
            indices = tuple(self.config["zones"][order.zone]["sku_bins"][line.sku]
                            for line in order.lines)
        allocation = OrderAllocation(order, indices)
        self.orders[order.order_id] = allocation
        for index in indices:
            resource = self.bins[f"{order.zone}{index + 1}"]
            resource.state, resource.order_id = "ALLOCATED", order.order_id
        return allocation

    def finish(self, order_id: str, status: str, motion_attempted: bool, entities: list) -> None:
        allocation = self.orders[order_id]
        allocation.entities = list(entities)
        if status == "COMPLETED":
            state = "WAITING_PICKUP"
        elif motion_attempted:
            state = "RECOVERY_REQUIRED"
        else:
            self._release(allocation)
            allocation.state = status
            return
        allocation.state = state
        for index in allocation.bin_indices:
            self.bins[f"{allocation.order.zone}{index + 1}"].state = state

    def pickup(self, order_id: str) -> OrderAllocation:
        allocation = self.orders.get(order_id)
        if allocation is None or allocation.state != "WAITING_PICKUP":
            raise ValueError("Only a completed order waiting for pickup can be collected")
        return allocation

    def confirm_pickup(self, order_id: str) -> None:
        allocation = self.pickup(order_id)
        self._release(allocation)
        allocation.state = "COLLECTED"

    def _release(self, allocation: OrderAllocation) -> None:
        for index in allocation.bin_indices:
            resource = self.bins[f"{allocation.order.zone}{index + 1}"]
            if resource.order_id != allocation.order.order_id:
                raise ValueError("Bin ownership mismatch")
            resource.state, resource.order_id = "FREE", None

    def can_reset(self) -> bool:
        return not any(b.state in ("WAITING_PICKUP", "ALLOCATED") for b in self.bins.values())

    def reset(self) -> None:
        if not self.can_reset():
            raise ValueError("Collect completed orders before resetting the scene")
        for resource in self.bins.values():
            resource.state, resource.order_id = "FREE", None
        # Retain used order IDs for this process lifetime.
        for allocation in self.orders.values():
            if allocation.state == "RECOVERY_REQUIRED":
                allocation.state = "DISCARDED_BY_RESET"

    def snapshot(self) -> list[dict]:
        return [dict(bin_id=b.bin_id, state=b.state, order_id=b.order_id)
                for b in self.bins.values()]
