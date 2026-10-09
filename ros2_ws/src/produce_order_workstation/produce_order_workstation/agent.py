"""Goal-driven tool-calling agent for the prepared produce workcell.

The model chooses among visual detections. Motion coordinates,
bin assignment, completion and safety checks remain deterministic tools.
"""

import json
import os
from pathlib import Path

from .order import OrderError, PerceptionMismatch


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_object_positions",
            "description": "Use the RGB-D camera, detection and segmentation services to recognize one requested SKU and calculate its grasp coordinates in the robot base frame. Call before each pick.",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pick_and_place",
            "description": "Pick one visual detection by detection_id, re-localize it with RGB-D, and put it in the receiving bin assigned by the workstation. The tool owns bin selection, motion and verification.",
            "parameters": {
                "type": "object",
                "properties": {"detection_id": {"type": "string"}},
                "required": ["detection_id"],
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
        if name == "get_object_positions" and set(arguments) == {"sku"}:
            return self.workstation.get_object_positions(self.order, arguments["sku"])
        if name == "pick_and_place" and set(arguments) == {"detection_id"}:
            return self.workstation.pick_and_place(self.order, arguments["detection_id"])
        if name == "finish_order" and not arguments:
            return self.workstation.finish_order(self.order)
        raise OrderError(f"Unknown tool or invalid tool arguments: {name}")

    def _remaining(self) -> dict[str, int]:
        return {
            line.sku: line.quantity - self.workstation._completed_by_sku.get(line.sku, 0)
            for line in self.order.lines
        }

    def _next_sku(self) -> str | None:
        return next((sku for sku, count in self._remaining().items() if count), None)

    def run(self) -> None:
        goal = {line.sku: line.quantity for line in self.order.lines}
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a produce order fulfillment robot agent. For a requested SKU, "
                    "first call get_object_positions to recognize visible items and "
                    "calculate their 3D positions from the RGB-D camera. Then choose "
                    "one returned detection_id for pick_and_place. Re-perceive after "
                    "every successful pick because coordinates become stale. If a "
                    "candidate fails the visual safety check, choose a different "
                    "detection or perceive again. A rejected or stale detection is "
                    "not a completed pick; follow the tool error and re-perceive. "
                    "Complete each requested SKU in order before switching to the next. "
                    "Fulfil all requested counts, then call finish_order. "
                    "Never invent IDs or coordinates. Call one tool at a time."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"goal": "Collect the requested produce from mixed table stock into workstation-assigned bins", "mode": self.workstation.config.get("operation_mode", "classification"), "zone": self.order.zone, "requested": goal,
                     "completed": dict(self.workstation._completed_by_sku), "remaining": self._remaining()},
                    ensure_ascii=False,
                ),
            },
        ]
        perceived_sku = None
        empty_perceptions = 0
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
                self.workstation.event(
                    self.order.order_id, "AGENT_RETRY",
                    reason="Expected exactly one tool call", received=len(calls),
                    remaining=self._remaining(), required_sku=self._next_sku(),
                )
                if calls:
                    messages.append({
                        "role": "assistant", "content": message.content,
                        "tool_calls": [
                            {"id": item.id, "type": "function", "function": {
                                "name": item.function.name,
                                "arguments": item.function.arguments,
                            }} for item in calls
                        ],
                    })
                    messages.extend({
                        "role": "tool", "tool_call_id": item.id,
                        "content": json.dumps({"ok": False, "retryable": True,
                                               "error": "Call exactly one tool; no action was executed"}),
                    } for item in calls)
                else:
                    messages.append({"role": "assistant", "content": message.content or ""})
                    messages.append({"role": "user", "content": "Call exactly one tool now."})
                continue
            call = calls[0]
            name = call.function.name
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise OrderError("Agent returned invalid tool JSON") from exc
            if not isinstance(arguments, dict):
                raise OrderError("Agent tool arguments must be an object")
            self.workstation.event(
                self.order.order_id, "AGENT_TOOL_CALL", tool=name, arguments=arguments
            )
            required_sku = self._next_sku()
            if name == "finish_order" and required_sku is not None:
                result = {
                    "ok": False, "retryable": True,
                    "error": "Requested quantities are not complete",
                    "remaining": self._remaining(), "required_sku": required_sku,
                }
            elif name == "get_object_positions" and arguments.get("sku") != required_sku:
                result = {
                    "ok": False, "retryable": True,
                    "error": "Complete the current SKU before switching",
                    "remaining": self._remaining(), "required_sku": required_sku,
                }
            elif perceived_sku is None and name == "pick_and_place":
                result = {
                    "ok": False,
                    "error": "Call get_object_positions before another pick; previous detections are stale",
                    "retryable": True,
                    "required_tool": "get_object_positions",
                    "required_sku": required_sku,
                }
            else:
                try:
                    result = self._invoke_tool(name, arguments)
                except PerceptionMismatch as exc:
                    perceived_sku = None
                    result = {"ok": False, "error": str(exc), "retryable": True,
                              "required_tool": "get_object_positions", "required_sku": required_sku}
            self.workstation.event(
                self.order.order_id, "AGENT_TOOL_RESULT", tool=name, result=result
            )
            if name == "get_object_positions" and "detections" in result:
                if result["detections"]:
                    perceived_sku = arguments["sku"]
                    empty_perceptions = 0
                else:
                    perceived_sku = None
                    empty_perceptions += 1
                    limit = self.config["max_consecutive_empty_perceptions"]
                    self.workstation.event(
                        self.order.order_id, "PERCEPTION_EMPTY",
                        sku=arguments["sku"], attempts=empty_perceptions,
                        limit=limit, remaining=self._remaining(),
                    )
                    if empty_perceptions >= limit:
                        self.workstation.event(
                            self.order.order_id, "PERCEPTION_EXHAUSTED",
                            sku=arguments["sku"], attempts=empty_perceptions,
                            remaining=self._remaining(),
                        )
                        raise OrderError(
                            f"No {arguments['sku']} detected after {limit} consecutive "
                            f"visual attempts; {self._remaining()[arguments['sku']]} still needed"
                        )
            elif name == "pick_and_place" and result.get("placed"):
                perceived_sku = None
                empty_perceptions = 0
            if name == "finish_order" and result.get("status") == "verified":
                return
            if name == "pick_and_place" and result.get("placed"):
                # A fresh goal summary avoids carrying old visual IDs and large
                # perception results through a long order.
                messages = messages[:2] + [{
                    "role": "user",
                    "content": json.dumps({
                        "remaining": self._remaining(),
                        "next_sku": self._next_sku(),
                        "instruction": "Get fresh object positions before the next pick; finish only when all counts are zero",
                    }, ensure_ascii=False),
                }]
                continue
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
