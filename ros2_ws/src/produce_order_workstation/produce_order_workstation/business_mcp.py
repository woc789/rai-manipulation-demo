"""Separate business capability: no motion, approval, reset or resource bypass."""
import asyncio
import json
import secrets
import threading
from mcp.server.fastmcp import FastMCP
from .mcp_server import WorkstationMCPServer, _request_capability
from .business import TOOL_DESCRIPTIONS


class BusinessMCPServer(WorkstationMCPServer):
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.token = secrets.token_urlsafe(32)
        self._lease = threading.Lock()
        self._accepting = True
        self._thread = self._socket = None
        self.mcp = FastMCP("Produce Business", json_response=True, stateless_http=True, log_level="WARNING")

        @self.mcp.tool(description=TOOL_DESCRIPTIONS["query_orders"])
        async def query_orders() -> dict:
            return await self.invoke("query_orders", {})

        @self.mcp.tool(description=TOOL_DESCRIPTIONS["query_inventory"])
        async def query_inventory(sku: str = "") -> dict:
            return await self.invoke("query_inventory", {"sku": sku})

        @self.mcp.tool(description=TOOL_DESCRIPTIONS["propose_fulfillment_plan"])
        async def propose_fulfillment_plan(order_id: str = "") -> dict:
            return await self.invoke("propose_fulfillment_plan", {"order_id": order_id})

        @self.mcp.tool(description=TOOL_DESCRIPTIONS["explain_task_status"])
        async def explain_task_status(order_id: str = "") -> dict:
            return await self.invoke("explain_task_status", {"order_id": order_id})

        @self.mcp.tool(description=TOOL_DESCRIPTIONS["request_order_change"])
        async def request_order_change(order_id: str, quantities: dict[str, int]) -> dict:
            return await self.invoke("request_order_change", {"order_id": order_id, "quantities": quantities})

        @self.mcp.tool(description=TOOL_DESCRIPTIONS["request_replenishment"])
        async def request_replenishment(sku: str = "") -> dict:
            return await self.invoke("request_replenishment", {"sku": sku})

        @self.mcp.resource("workstation://business/status")
        def status() -> str:
            if _request_capability.get() != self.token or not self._accepting:
                raise ValueError("Business capability expired")
            return json.dumps(self.coordinator.call("explain_task_status", {}), ensure_ascii=False)

    async def invoke(self, command, args):
        def execute():
            with self._lease:
                if not self._accepting or _request_capability.get() != self.token:
                    return {"error": {"kind": "OrderError", "message": "Business capability expired"}}
                try:
                    self.coordinator.server.store.event({"stage": "BUSINESS_TOOL_CALL", "tool": command})
                    result = self.coordinator.call(command, args)
                    self.coordinator.server.store.event({"stage": "BUSINESS_TOOL_RESULT", "tool": command, "ok": True})
                    return {"result": result}
                except Exception as exc:
                    self.coordinator.server.store.event({"stage": "BUSINESS_TOOL_RESULT", "tool": command, "ok": False, "reason": str(exc)})
                    return {"error": {"kind": "OrderError", "message": str(exc)}}
        return await asyncio.to_thread(execute)
