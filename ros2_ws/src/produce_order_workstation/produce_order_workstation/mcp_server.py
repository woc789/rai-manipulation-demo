"""Order-scoped MCP facade. ROS execution and resource ownership stay in Workstation."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import secrets
import socket
import threading
import time

from mcp.server.fastmcp import FastMCP
import uvicorn

from .order import OrderError, PerceptionMismatch
from .execution_gate import execution_gate


_request_capability = ContextVar("workstation_mcp_capability", default=None)


class _LocalTokenGuard:
    """Ephemeral local capability, not a public OAuth authorization service."""

    def __init__(self, app, token):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            expected = f"Bearer {self.token()}".encode()
            if not secrets.compare_digest(headers.get(b"authorization", b""), expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
            marker = _request_capability.set(headers[b"authorization"].decode()[7:])
            try:
                await self.app(scope, receive, send)
            finally:
                _request_capability.reset(marker)
            return
        await self.app(scope, receive, send)


class WorkstationMCPServer:
    """One admitted order, one serial tool lease, loopback-only HTTP endpoint.

    The token and order are supplied by the host, never by the LLM. Blocking ROS
    operations run off the HTTP loop. Shutdown drains them: an HTTP timeout is
    NOT proof that a physical operation stopped. No automatic motion retries.
    """

    def __init__(self, workstation, order=None):
        self.workstation = workstation
        self.order = order
        self.token = secrets.token_urlsafe(32)
        self._lease = threading.Lock()
        self._accepting = True
        self._perceived = False
        self._motion_allowed = True
        self._execution_gate = execution_gate(workstation)
        self._thread = None
        self._socket = None
        self.mcp = FastMCP("Produce Workstation", json_response=True,
                           stateless_http=True, log_level="WARNING")

        @self.mcp.tool()
        async def get_object_positions(sku: str) -> dict:
            """Recognize a requested SKU with RGB-D and return detection IDs and 3D grasp positions. Call before each pick."""
            return await asyncio.to_thread(self._execute, "get_object_positions", {"sku": sku})

        @self.mcp.tool()
        async def pick_and_place(detection_id: str) -> dict:
            """Pick one returned visual detection into this order's assigned bin. Workstation owns coordinates, motion and verification."""
            return await asyncio.to_thread(self._execute, "pick_and_place", {"detection_id": detection_id})

        @self.mcp.tool()
        async def finish_order() -> dict:
            """Audit placed objects and finish only when every requested quantity is met."""
            return await asyncio.to_thread(self._execute, "finish_order", {})

        @self.mcp.resource("workstation://order/context")
        def order_context() -> str:
            """Read-only context for the order bound by the host, not a model-selected order."""
            if self.order is None or _request_capability.get() != self.token:
                raise OrderError("No admitted order is bound to this MCP server")
            return json.dumps({
                "order_id": self.order.order_id,
                "zone": self.order.zone,
                "requested": {line.sku: line.quantity for line in self.order.lines},
                "mode": workstation.config.get("operation_mode", "classification"),
                "completed": dict(workstation._completed_by_sku),
                "candidate_ids": list(workstation._visual_candidates),
            })

    def _execute(self, name, arguments):
        if not self._lease.acquire(blocking=False):
            return {"error": {"kind": "OrderError", "message": "Another workstation tool is in flight"}}
        try:
            if self.order is None or _request_capability.get() != self.token:
                return {"error": {"kind": "OrderError", "message": "No admitted order is bound"}}
            if not self._accepting:
                raise OrderError("MCP order session is closed")
            if name in ("pick_and_place", "finish_order") and not self._motion_allowed:
                raise OrderError("Behavior-tree session permits MCP perception only; physical skills belong to the tree")
            with self._execution_gate.hold():
                return self._execute_admitted(name, arguments)
        except OrderError as exc:
            return {"error": {"kind": type(exc).__name__, "message": str(exc)}}
        finally:
            self._lease.release()

    def _execute_admitted(self, name, arguments):
        try:
            self.workstation.check_cancel()
            self.workstation.event(self.order.order_id, "MCP_TOOL_CALL", tool=name)
            if name == "get_object_positions":
                self._perceived = False
                sku = arguments["sku"]
                remaining = {line.sku: line.quantity - self.workstation._completed_by_sku.get(line.sku, 0)
                             for line in self.order.lines}
                if remaining.get(sku, 0) <= 0:
                    raise OrderError("SKU is not needed by the bound order")
                result = self.workstation.get_object_positions(self.order, sku)
                self._perceived = bool(result.get("detections"))
            elif name == "pick_and_place":
                if not self._perceived:
                    raise OrderError("Call get_object_positions before moving; previous detections are stale")
                result = self.workstation.pick_and_place(self.order, arguments["detection_id"])
                if result.get("placed"):
                    self._perceived = False
            elif name == "finish_order":
                result = self.workstation.finish_order(self.order)
            else:
                raise OrderError("Unknown workstation tool")
            self.workstation.event(self.order.order_id, "MCP_TOOL_RESULT", tool=name, ok=True)
            return {"result": result}
        except OrderError as exc:
            error = {"kind": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, PerceptionMismatch):
                error.update(code=exc.code, reobserve=exc.reobserve)
                if exc.reobserve:
                    self._perceived = False
            self.workstation.event(self.order.order_id, "MCP_TOOL_RESULT", tool=name,
                                   ok=False, reason=str(exc))
            return {"error": error}
        except Exception as exc:
            # Keep the underlying failure in the existing event/report path;
            # FastMCP's generic tool-error flag alone would hide this diagnosis.
            self.workstation.event(self.order.order_id, "MCP_TOOL_RESULT", tool=name,
                                   ok=False, reason=str(exc), error_type=type(exc).__name__)
            return {"error": {"kind": "OrderError", "message": str(exc)}}

    def __enter__(self):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        port = self._socket.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        app = _LocalTokenGuard(self.mcp.streamable_http_app(), lambda: self.token)
        self._server = uvicorn.Server(uvicorn.Config(app, access_log=False,
            log_level="warning", lifespan="on", timeout_graceful_shutdown=None))
        self._thread = threading.Thread(target=self._server.run,
            kwargs={"sockets": [self._socket]}, name="workstation-mcp-server", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() >= deadline:
                self.close()
                raise OrderError("Workstation MCP server failed to start")
            time.sleep(.01)
        return self

    @contextmanager
    def order_session(self, order, *, allow_motion=True):
        """Keep HTTP alive, rotate capability at each admitted task boundary."""
        with self._lease:
            if self.order is not None or not self._accepting:
                raise OrderError("Workstation MCP has an active order or is closed")
            self.order = order
            self.token = secrets.token_urlsafe(32)
            self._perceived = False
            self._motion_allowed = allow_motion
        try:
            yield self
        finally:
            # Drain late physical calls before admitting another order.
            with self._lease:
                self.order = None
                self.token = secrets.token_urlsafe(32)
                self._perceived = False
                self._motion_allowed = False

    def close(self):
        self._accepting = False
        # Do not release the workstation to another order while a tool is moving it.
        with self._lease:
            pass
        if self._thread is not None:
            self._server.should_exit = True
            self._thread.join()
        if self._socket is not None:
            self._socket.close()

    def __exit__(self, *_):
        self.close()
