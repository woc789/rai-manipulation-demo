from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from produce_order_interfaces.srv import FulfillmentStep
from produce_order_workstation.execution_bridge import ExecutionBridge
from produce_order_workstation.order import Order, OrderLine, PerceptionMismatch, OrderPaused
from produce_order_workstation.persistence import WorkcellStore


@pytest.fixture
def gateway(tmp_path):
    bridge = ExecutionBridge.__new__(ExecutionBridge)
    bridge._lock = Lock()
    station = MagicMock()
    station._completed_by_sku = {"corn": 0}
    station.physical_state_uncertain = False
    station.get_object_positions.return_value = {"detections": [{"detection_id": "v1"}]}
    station._visual_candidates = {"v1": ("corn", (.5,0,.05))}
    station._agent_dispatcher.execute.return_value = "v1"
    bridge.station = station
    bridge.server = SimpleNamespace(store=WorkcellStore(tmp_path/"step.db"),persist_state=lambda: None)
    agent = MagicMock()
    agent.select_candidate.return_value = "v1"
    bridge._active = dict(token="secret",order=Order("A","A",(OrderLine("corn",1),)),
        agent=agent, sequence=0,cache={},cancelled=False,terminal=False,last_candidate="",alternatives=False)
    yield bridge
    bridge.server.store.close()


def call(gateway, command, sequence=0, candidate="", token="secret"):
    return gateway.step(FulfillmentStep.Request(session_id=token,sequence=sequence,
        command=command,detection_id=candidate),FulfillmentStep.Response())


def test_expired_session_cannot_move(gateway):
    assert call(gateway,"pick",candidate="v1",token="wrong").status == "FAILED"
    gateway.station.pick_and_place.assert_not_called()


def test_sequence_and_selection_gate(gateway):
    assert call(gateway,"pick",sequence=4,candidate="v1").status == "FAILED"
    assert call(gateway,"pick",candidate="invented").status == "FAILED"
    gateway.station.pick_and_place.assert_not_called()


def test_repeated_step_returns_cached_result_without_motion(gateway):
    assert call(gateway,"select").detection_id == "v1"
    def placed(*_):
        gateway.station._completed_by_sku["corn"] = 1
    gateway.station.pick_and_place.side_effect = placed
    assert call(gateway,"pick",1,"v1").status == "SUCCESS"
    assert call(gateway,"pick",1,"v1").status == "SUCCESS"
    gateway.station.pick_and_place.assert_called_once()
    assert gateway.server.store.operations()[0]["state"] == "VERIFIED"


def test_planning_rejection_is_bounded_retry_not_false_completion(gateway):
    call(gateway,"select")
    gateway.station.pick_and_place.side_effect = PerceptionMismatch("safe reject",code="MOTION_PLAN_REJECTED",reobserve=False)
    result = call(gateway,"pick",1,"v1")
    assert result.status == "RETRY"
    assert gateway.station._completed_by_sku["corn"] == 0
    assert gateway.server.store.operations()[0]["state"] == "NO_EFFECT"


def test_safe_yield_records_verified_operation(gateway):
    call(gateway,"select")
    def paused(*_):
        gateway.station._completed_by_sku["corn"] = 1
        raise OrderPaused("safe yield")
    gateway.station.pick_and_place.side_effect = paused
    assert call(gateway,"pick",1,"v1").status == "PAUSED"
    assert gateway.server.store.operations()[0]["state"] == "VERIFIED"


def test_uncertain_motion_never_returns_retry(gateway):
    call(gateway,"select")
    def fail(*_):
        gateway.station.physical_state_uncertain = True
        raise RuntimeError("lost result")
    gateway.station.pick_and_place.side_effect = fail
    assert call(gateway,"pick",1,"v1").status == "FAILED"
    assert gateway.server.store.operations()[0]["state"] == "UNCERTAIN"
