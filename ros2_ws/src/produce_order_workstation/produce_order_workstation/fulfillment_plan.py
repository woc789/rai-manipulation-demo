"""Constrained Agent plan contract and deterministic BT compiler.

The model may choose order and perception retry budgets, never ROS commands,
poses, candidate IDs, XML, or completion claims.
"""

from collections import Counter
import hashlib
import json
from xml.etree import ElementTree as ET

from .order import OrderError


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def context_digest(context):
    return hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()


def planning_context(order, station, max_attempts):
    completed = dict(station._completed_by_sku)
    observations = station.inventory.snapshot()
    counts = {line.sku: {"available": 0, "reserved": 0, "uncertain": 0}
              for line in order.lines}
    for item in observations["items"]:
        bucket = counts.get(item["sku"])
        if bucket is None:
            continue
        state = item["state"]
        if state == "AVAILABLE" and not item["stale"]:
            bucket["available"] += 1
        elif state == "RESERVED" and item["order_id"] == order.order_id and not item["stale"]:
            bucket["reserved"] += 1
        elif state == "UNKNOWN" or item["stale"]:
            bucket["uncertain"] += 1
    return {
        "version": 1, "order_id": order.order_id, "zone": order.zone,
        "requested": {line.sku: line.quantity for line in order.lines},
        "completed": completed,
        "remaining": {line.sku: line.quantity - completed.get(line.sku, 0) for line in order.lines},
        "bin_id": (f"{order.zone}{station._allocated_bin_index + 1}"
                   if station._allocated_bin_index is not None else "classification/default"),
        "inventory_observation": {"epoch": observations["epoch"], "counts": counts,
                                  "warning": "Counts are observations, not guaranteed stock; execution must re-perceive."},
        "capabilities": ["select_observed_candidate", "pick_and_place_verified", "audit_order"],
        "limits": {"max_visual_attempts_per_item": int(max_attempts)},
    }


def validate_plan(plan, context):
    if not isinstance(plan, dict) or set(plan) != {"version", "context_digest", "items"}:
        raise OrderError("Agent plan has unknown or missing fields")
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise OrderError("Unsupported Agent plan version")
    if plan["context_digest"] != context_digest(context):
        raise OrderError("Agent plan was made for a different context")
    items = plan["items"]
    if not isinstance(items, list) or len(items) != sum(context["remaining"].values()):
        raise OrderError("Agent plan does not cover every remaining item exactly once")
    expected = Counter({sku: qty for sku, qty in context["remaining"].items() if qty > 0})
    actual = Counter()
    limit = context["limits"]["max_visual_attempts_per_item"]
    for item in items:
        if not isinstance(item, dict) or set(item) != {"sku", "visual_attempts"}:
            raise OrderError("Agent plan item has unknown or missing fields")
        if item["sku"] not in expected or type(item["visual_attempts"]) is not int or not 1 <= item["visual_attempts"] <= limit:
            raise OrderError("Agent plan requested an unauthorized SKU or retry budget")
        actual[item["sku"]] += 1
    if actual != expected:
        raise OrderError("Agent plan changes the order quantities")
    return plan


def compile_tree(plan, context):
    """Only this compiler creates executable BT XML; no model-provided XML is used."""
    validate_plan(plan, context)
    root = ET.Element("root", BTCPP_format="4", main_tree_to_execute="AgentPlan")
    sequence = ET.SubElement(ET.SubElement(root, "BehaviorTree", ID="AgentPlan"), "Sequence")
    for item in plan["items"]:
        retry = ET.SubElement(sequence, "RetryUntilSuccessful", num_attempts=str(item["visual_attempts"]))
        pair = ET.SubElement(retry, "Sequence")
        ET.SubElement(pair, "Capability", command="select", target_sku=item["sku"])
        ET.SubElement(pair, "Capability", command="pick", target_sku=item["sku"])
    ET.SubElement(sequence, "Capability", command="audit")
    return ET.tostring(root, encoding="unicode")
