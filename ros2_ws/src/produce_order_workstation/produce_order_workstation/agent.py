"""Goal-driven tool-calling agent for the prepared produce workcell.

The model decides *which* named object to handle next. Motion coordinates,
bin assignment, completion and safety checks remain deterministic tools.
"""

import json
import os
from pathlib import Path

from .order import OrderError


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_scene",
            "description": "Observe named objects still on the table and current order progress. Call this first and after a failed action.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pick_and_place",
            "description": "Pick one observed object by entity_id and put it in its fixed SKU bin. The tool owns all motion, target and verification logic.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_order",
            "description": "Audit all placed objects and finish only when every requested quantity is met.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _key_from_private_file() -> str | None:
    """Read the existing user-local key file without executing shell code."""
    path = Path.home() / ".config/rai/bailian.env"
    if not path.is_file() or path.stat().st_mode & 0o077:
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.removeprefix("export ").strip()
        if line.startswith("DASHSCOPE_API_KEY="):
            return line.partition("=")[2].strip().strip("\"'")
    return None


def create_model(config: dict):
    """Use OpenAI-compatible tool calling, defaulting to the user's Bailian key."""
    from openai import OpenAI

    agent_config = config["agent"]
    key = os.getenv("DASHSCOPE_API_KEY") or _key_from_private_file()
    if not key:
        raise OrderError(
            "Agent unavailable: set DASHSCOPE_API_KEY or create a mode-600 "
            "~/.config/rai/bailian.env file"
        )
    return OpenAI(
        api_key=key,
        base_url=os.getenv("PRODUCE_AGENT_BASE_URL", agent_config["base_url"]),
        timeout=agent_config["timeout_s"],
        max_retries=1,
    )


class ProduceAgent:
    """One bounded model/tool loop; there is no scripted picking fallback."""

    def __init__(self, workstation, order, model=None):
        self.workstation = workstation
        self.order = order
        self.config = workstation.config["agent"]
        self.model = model if model is not None else create_model(workstation.config)
        self.model_name = os.getenv("PRODUCE_AGENT_MODEL", self.config["model"])

    def _invoke_tool(self, name: str, arguments: dict) -> dict:
        if name == "inspect_scene" and not arguments:
            return self.workstation.inspect_scene(self.order)
        if name == "pick_and_place" and set(arguments) == {"entity_id"}:
            return self.workstation.pick_and_place(self.order, arguments["entity_id"])
        if name == "finish_order" and not arguments:
            return self.workstation.finish_order(self.order)
        raise OrderError(f"Unknown tool or invalid tool arguments: {name}")

    def run(self) -> None:
        goal = {line.sku: line.quantity for line in self.order.lines}
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a produce-sorting robot agent. First call inspect_scene. "
                    "Use only the returned entity IDs to choose which physical item to pick. "
                    "Fulfil the requested counts, and then call finish_order. "
                    "Never invent stock, coordinates or tool results. Do not pick extra items. "
                    "Each SKU has a fixed bin handled by the tool. Call one tool at a time."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"goal": "Sort the requested produce from mixed table stock into its dedicated bins", "zone": self.order.zone, "requested": goal},
                    ensure_ascii=False,
                ),
            },
        ]
        inspected = False
        max_calls = self.config["max_tool_calls_per_item"] * self.order.total_items + 6
        for _ in range(max_calls):
            self.workstation.check_cancel()
            response = self.model.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0,
            )
            message = response.choices[0].message
            calls = message.tool_calls or []
            if len(calls) != 1:
                raise OrderError("Agent must select exactly one tool per step")
            call = calls[0]
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise OrderError("Agent returned invalid tool JSON") from exc
            if not isinstance(arguments, dict):
                raise OrderError("Agent tool arguments must be an object")
            if not inspected and name != "inspect_scene":
                raise OrderError("Agent must inspect the scene before moving")
            self.workstation.event(
                self.order.order_id, "AGENT_TOOL_CALL", tool=name, arguments=arguments
            )
            result = self._invoke_tool(name, arguments)
            self.workstation.event(
                self.order.order_id, "AGENT_TOOL_RESULT", tool=name, result=result
            )
            if name == "inspect_scene":
                inspected = True
            if name == "finish_order":
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": name, "arguments": call.function.arguments},
                        }
                    ],
                }
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
        raise OrderError("Agent exceeded its bounded tool-call budget")
