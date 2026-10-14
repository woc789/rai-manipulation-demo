"""Admitted-session capability gateway for the non-blocking C++ behavior tree.

The gateway serializes skills, rejects late sessions and caches step replies.
Cancelling the tree never implies an in-flight skill has stopped: the owner
waits for the gateway to drain before releasing its physical execution lease.
"""

import time
import uuid
from threading import Lock

from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from produce_order_interfaces.action import RunFulfillment
from produce_order_interfaces.srv import FulfillmentStep

from .order import OrderError, OrderCancelled, OrderPaused, PerceptionMismatch
from .execution_gate import execution_gate


class ExecutionBridge:
    def __init__(self, server):
        self.server = server
        self.station = server.workstation
        self._lock = Lock()
        self._active = None
        self._service = server.create_service(FulfillmentStep, "/produce_orders/internal/step",
                                              self.step, callback_group=ReentrantCallbackGroup())
        self._client = ActionClient(server, RunFulfillment, "/produce_orders/internal/run_tree",
                                    callback_group=ReentrantCallbackGroup())

    def run(self, order):
        token = uuid.uuid4().hex
        with self._lock:
            self._active = dict(token=token, order=order,
                                sequence=0, cache={}, cancelled=False, terminal=False,
                                last_candidate="", alternatives=False)
        original_cancel = self.station.cancel_requested
        self.station.cancel_requested = lambda: original_cancel() or bool(self._active and self._active["cancelled"])
        handle = None
        try:
            if not self._client.wait_for_server(timeout_sec=5):
                raise OrderError("Behavior-tree executor unavailable; no scripted fallback")
            goal = RunFulfillment.Goal(session_id=token,
                max_attempts=int(self.station.config["agent"]["max_consecutive_empty_perceptions"]))
            accepted = self._client.send_goal_async(goal, feedback_callback=lambda message:
                self.station.event(order.order_id, "BT_STAGE", command=message.feedback.stage))
            deadline = time.monotonic()+15
            while not accepted.done():
                if time.monotonic() > deadline:
                    raise OrderError("Behavior-tree admission timeout")
                time.sleep(.02)
            handle = accepted.result()
            if not handle.accepted:
                raise OrderError("Behavior-tree executor rejected session")
            future = handle.get_result_async()
            deadline = time.monotonic()+float(self.station.config.get("scheduler", {}).get("result_timeout_s", 900))
            cancel_sent = False
            while not future.done():
                if original_cancel() or time.monotonic() > deadline:
                    self._active["cancelled"] = True
                    if not cancel_sent:
                        handle.cancel_goal_async()
                        cancel_sent = True
                if time.monotonic() > deadline+20:
                    self.station.physical_state_uncertain = True
                    raise OrderError("Behavior-tree result lost after timeout; reconcile physical state")
                time.sleep(.02)
            result = future.result().result
            if result.status == "PAUSED":
                raise OrderPaused(result.error)
            if result.status == "CANCELLED" or original_cancel():
                raise OrderCancelled(result.error or "Cancelled at a safe skill boundary")
            if result.status != "COMPLETED":
                raise OrderError(result.error or "Behavior tree failed")
        finally:
            # Mark cancellation BEFORE waiting for any active callback to drain.
            # Never clear the workstation context while a skill can still move it.
            if self._active:
                self._active["cancelled"] = True
            with self._lock:
                self._active = None
            self.station.cancel_requested = original_cancel

    def step(self, request, response):
        if request.command in ("pick", "audit"):
            try:
                with execution_gate(self.station).hold():
                    return self._step(request, response)
            except OrderError as exc:
                response.status, response.reason = "FAILED", str(exc)
                return response
        return self._step(request, response)

    @staticmethod
    def _remember(session, key, response):
        session["sequence"] = max(session["sequence"], key[0] + 1)
        session["cache"][key] = {name: getattr(response, name)
            for name in ("status", "reason", "remaining", "detection_id")}

    def _step(self, request, response):
        with self._lock:
            session = self._active
            if session is None or request.session_id != session["token"]:
                response.status, response.reason = "FAILED", "Invalid or expired execution session"
                return response
            key = (request.sequence, request.command, request.detection_id)
            if key in session["cache"]:
                for name, value in session["cache"][key].items():
                    setattr(response, name, value)
                return response
            order = session["order"]
            if request.command == "pick":
                saved = self.server.store.operation(f"{session['token']}:{request.sequence}")
                if saved is not None:
                    result = saved.get("result") or {}
                    fields = ("status", "reason", "remaining", "detection_id")
                    if (saved["order_id"] == order.order_id and saved["state"] in ("VERIFIED", "NO_EFFECT")
                        and result.get("request_detection_id") == request.detection_id and all(k in result for k in fields)):
                        for name in fields:
                            setattr(response, name, result[name])
                        self._remember(session, key, response)
                        if response.status in ("FAILED", "CANCELLED", "PAUSED"):
                            session["terminal"] = True
                        return response
                    response.status, response.reason = "FAILED", "Existing operation requires reconciliation; never replay motion"
                    session["terminal"] = True
                    return response
            if request.sequence != session["sequence"] or session["terminal"]:
                response.status, response.reason = "FAILED", "Out-of-order or terminal execution request"
                return response
            operation = None
            before_completed = sum(self.station._completed_by_sku.values())
            started = time.monotonic()
            try:
                self.station.check_cancel()
                response.remaining = sum(max(0, line.quantity-self.station._completed_by_sku.get(line.sku, 0))
                                         for line in order.lines)
                if request.command == "select":
                    sku = next((line.sku for line in order.lines
                                if self.station._completed_by_sku.get(line.sku, 0) < line.quantity), None)
                    if sku is None:
                        response.status = "DONE"
                    else:
                        gateway = self.station._mcp_server
                        selected = self.station._agent_dispatcher.execute(
                            order, gateway.url, gateway.token, mode="select")
                        if not selected:
                            response.status, response.reason = "RETRY", f"No selectable {sku} candidate"
                        else:
                            if selected not in self.station._visual_candidates:
                                raise OrderError("Agent returned a candidate outside the current visual set")
                            response.detection_id = selected
                            session["last_candidate"] = response.detection_id
                            response.status = "SUCCESS"
                elif request.command == "pick":
                    if request.detection_id != session["last_candidate"] or not request.detection_id:
                        raise OrderError("Pick requires the selected candidate from this session")
                    operation = self.server.store.begin_operation(order.order_id, "PICK_AND_PLACE",
                        f"{session['token']}:{request.sequence}")
                    self.station.pick_and_place(order, request.detection_id)
                    if sum(self.station._completed_by_sku.values()) <= before_completed:
                        raise OrderError("Pick returned without verified progress; cannot count completion")
                    session["alternatives"] = False
                    response.status = "SUCCESS"
                    response.remaining = order.total_items-sum(self.station._completed_by_sku.values())
                elif request.command == "audit":
                    self.station.finish_order(order)
                    response.status = "SUCCESS"
                else:
                    raise OrderError("Unknown behavior-tree capability")
            except PerceptionMismatch as exc:
                response.status = "FAILED" if self.station.physical_state_uncertain else "RETRY"
                response.reason = f"{exc.code}: {exc}"
                if self.station.physical_state_uncertain:
                    session["terminal"] = True
                session["alternatives"] = not exc.reobserve and bool(self.station._visual_candidates)
            except OrderPaused as exc:
                response.status, response.reason = "PAUSED", str(exc)
                session["terminal"] = True
            except OrderCancelled as exc:
                response.status, response.reason = "CANCELLED", str(exc)
                session["terminal"] = True
            except Exception as exc:
                response.status, response.reason = "FAILED", str(exc)
                session["terminal"] = True
            finally:
                if operation:
                    state = "VERIFIED" if sum(self.station._completed_by_sku.values()) > before_completed and not self.station.physical_state_uncertain else (
                        "UNCERTAIN" if self.station.physical_state_uncertain else "NO_EFFECT")
                    result = {name: getattr(response, name) for name in ("status", "reason", "remaining", "detection_id")}
                    result.update(request_detection_id=request.detection_id,
                                  completed_by_sku=dict(self.station._completed_by_sku))
                    try:
                        self.server.store.finish_operation(operation, state, result)
                    except Exception as exc:
                        self.station.physical_state_uncertain = True
                        session["terminal"] = True
                        raise OrderError("Operation journal failed; reconcile before retrying") from exc
                # Cache before snapshot/event IO: an IO failure cannot erase the
                # confirmed result or turn a repeated request into another pick.
                self._remember(session, key, response)
                self.station.event(order.order_id, "BT_STEP", command=request.command,
                                   status=response.status, reason=response.reason,
                                   duration_s=round(time.monotonic()-started, 3), operation_id=operation)
                self.server.persist_state()
            return response

    def close(self):
        self._client.destroy()
        self.server.destroy_service(self._service)
