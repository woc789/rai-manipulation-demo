"""One workcell-wide lease, shared by MCP, BT and direct skill calls."""
from contextlib import contextmanager
from threading import Lock, RLock
from functools import wraps
from .order import OrderError

_creation_lock = Lock()


class ExecutionGate:
    def __init__(self):
        self._lock = RLock()

    @contextmanager
    def hold(self):
        if not self._lock.acquire(blocking=False):
            raise OrderError("Workcell skill busy; do not overlap perception or physical execution")
        try:
            yield
        finally:
            self._lock.release()


def execution_gate(station):
    with _creation_lock:
        gate = getattr(station, "_execution_gate", None)
        if not isinstance(gate, ExecutionGate):
            gate = station._execution_gate = ExecutionGate()
        return gate


def serialized_skill(function):
    @wraps(function)
    def guarded(station, *args, **kwargs):
        with execution_gate(station).hold():
            return function(station, *args, **kwargs)
    return guarded
