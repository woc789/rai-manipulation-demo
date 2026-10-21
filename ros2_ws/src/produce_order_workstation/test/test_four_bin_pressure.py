"""Pressure-controller resumption contracts; no live simulator is invoked."""

import importlib.util
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "four_bin_pressure",
    Path(__file__).resolve().parents[1] / "scripts/check_four_bin_cycles_live.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_all_four_boxes_must_be_complete_before_pickup():
    jobs = {str(i): {"state": "WAITING_PICKUP"} for i in range(4)}
    assert probe.ready_for_pickup(jobs)
    jobs["3"]["state"] = "RUNNING"
    assert not probe.ready_for_pickup(jobs)


def test_partially_collected_cycle_can_resume_without_replaying_orders():
    jobs = {str(i): {"state": "WAITING_PICKUP"} for i in range(4)}
    jobs["0"]["state"] = "COLLECTED"
    assert probe.ready_for_pickup(jobs)
    del jobs["3"]
    assert not probe.ready_for_pickup(jobs)


def test_failed_order_cannot_be_counted_as_complete():
    jobs = {str(i): {"state": "COLLECTED"} for i in range(4)}
    jobs["3"]["state"] = "FAILED"
    assert not probe.ready_for_pickup(jobs)
