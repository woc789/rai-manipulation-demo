"""Synchronous Agent adapter around the official asynchronous MCP client."""

import asyncio
from concurrent.futures import Future
from datetime import timedelta
import json
import threading

import httpx
from jsonschema import validate, ValidationError
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .order import MotionPlanRejected, OrderCancelled, OrderError, OrderPaused, PerceptionMismatch


class WorkstationMCPClient:
    """Discover tools, invoke them over HTTP, and preserve domain errors.

    One worker task owns both async context managers (required by AnyIO).
    Calls are serialized and never retried, particularly after transport errors.
    """

    def __init__(self, url, token, *, timeout_s=900):
        self.url = url
        self._token = token
        self.timeout_s = timeout_s
        self._ready = Future()
        self._closed = False
        self._thread = None
        self._pending = set()
        self._pending_lock = threading.Lock()
        self._dead = False

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        try:
            # Ignore desktop proxy variables for the loopback robot endpoint.
            async with (
                httpx.AsyncClient(headers={"Authorization": f"Bearer {self._token}"},
                                  timeout=self.timeout_s, trust_env=False) as http_client,
                streamable_http_client(self.url, http_client=http_client) as (read, write, _),
            ):
                async with ClientSession(read, write,
                        read_timeout_seconds=timedelta(seconds=self.timeout_s)) as session:
                    await asyncio.wait_for(session.initialize(), timeout=10)
                    listed = await asyncio.wait_for(session.list_tools(), timeout=10)
                    self._schemas = {tool.name: {**tool.inputSchema, "additionalProperties": False}
                                     for tool in listed.tools}
                    self.tools = [{"type": "function", "function": {
                        "name": tool.name, "description": tool.description or "",
                        "parameters": self._schemas[tool.name],
                    }} for tool in listed.tools]
                    self._ready.set_result(None)
                    while True:
                        job = await self._queue.get()
                        if job is None:
                            break
                        method, args, future = job
                        try:
                            future.set_result(await getattr(session, method)(*args))
                        except Exception as exc:
                            future.set_exception(OrderError(
                                f"MCP transport failed; do not automatically repeat physical actions: {type(exc).__name__}"
                            ))
                        finally:
                            with self._pending_lock:
                                self._pending.discard(future)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(OrderError(
                    f"Cannot connect to workstation MCP server: {type(exc).__name__}"
                ))
        finally:
            with self._pending_lock:
                self._dead = True
                for future in self._pending:
                    if not future.done():
                        future.set_exception(OrderError("MCP client connection closed"))
                self._pending.clear()

    def __enter__(self):
        self._thread = threading.Thread(target=lambda: asyncio.run(self._serve()),
                                       name="workstation-mcp-client", daemon=True)
        self._thread.start()
        try:
            self._ready.result(timeout=15)
        except Exception:
            self.close()
            raise
        return self

    def _request(self, method, *args):
        if self._closed or self._thread is None or not self._thread.is_alive():
            raise OrderError("MCP client is closed")
        future = Future()
        with self._pending_lock:
            if self._dead:
                raise OrderError("MCP client connection closed")
            self._pending.add(future)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (method, args, future))
        except RuntimeError as exc:
            with self._pending_lock:
                self._pending.discard(future)
            raise OrderError("MCP client connection closed") from exc
        return future.result()

    def call(self, name, arguments):
        if name not in self._schemas:
            raise OrderError(f"Unknown MCP tool: {name}")
        try:
            validate(arguments, self._schemas[name])
        except ValidationError as exc:
            raise OrderError(f"Invalid MCP tool arguments: {name}") from exc
        response = self._request("call_tool", name, arguments)
        if response.isError:
            raise OrderError(f"MCP server rejected tool: {name}")
        payload = response.structuredContent
        if payload is None:
            payload = json.loads(next(block.text for block in response.content if block.type == "text"))
        if "error" in payload:
            error = payload["error"]
            if error["kind"] == "PerceptionMismatch":
                raise PerceptionMismatch(error["message"], code=error["code"], reobserve=error["reobserve"])
            error_class = {"OrderCancelled": OrderCancelled, "OrderPaused": OrderPaused,
                           "MotionPlanRejected": MotionPlanRejected}.get(error["kind"], OrderError)
            raise error_class(error["message"])
        return payload["result"]

    def context(self):
        response = self._request("read_resource", "workstation://order/context")
        return json.loads(response.contents[0].text)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._thread is not None:
            if self._thread.is_alive() and hasattr(self, "_queue") and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._thread.join()

    def __exit__(self, *_):
        self.close()
