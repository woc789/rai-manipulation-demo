"""Goal-driven tool-calling agent for the prepared produce workcell.

The model chooses among visual detections. Motion coordinates,
bin assignment, completion and safety checks remain deterministic tools.
"""

import json
import os
from pathlib import Path
from typing import Callable, Protocol

from .order import Order, OrderCancelled, OrderError, PerceptionMismatch
from .fulfillment_plan import context_digest, validate_plan


class AgentToolClient(Protocol):
    """Capabilities the decision loop needs, not a concrete Workstation or SDK."""

    tools: list[dict]

    def call(self, name: str, arguments: dict) -> dict: ...

    def context(self) -> dict: ...


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


def create_model(agent_config: dict):
    """Use OpenAI-compatible tool calling, defaulting to the user's Bailian key."""
    from openai import OpenAI
    import httpx

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
        # systemd does not inherit desktop proxy variables. Configure this
        # remote-only client explicitly; MCP has its own direct HTTP client.
        http_client=httpx.Client(
            proxy=os.getenv("PRODUCE_AGENT_PROXY_URL") or agent_config.get("proxy_url"),
            trust_env=False,
        ),
    )


def explain_model_error(exc):
    """Expose transport cause types, never request headers, body or API keys."""
    from openai import APIConnectionError, APIStatusError

    if isinstance(exc, APIConnectionError):
        causes, current, dns_failure = [], exc, False
        for _ in range(6):
            if current is None:
                break
            causes.append(type(current).__name__)
            dns_failure = dns_failure or "Temporary failure in name resolution" in str(current)
            current = current.__cause__
        return ("模型域名 DNS 解析失败（" if dns_failure else "模型网络连接失败（") + " → ".join(causes) + "）；这不是 Key 鉴权失败的证据"
    if isinstance(exc, APIStatusError):
        return f"模型接口返回 HTTP {exc.status_code}；请按状态码检查鉴权、限流或服务状态"
    return str(exc)


class ProduceAgent:
    """One bounded model/tool loop; there is no scripted picking fallback."""

    def __init__(
        self, order: Order, config: dict, tool_client: AgentToolClient, *,
        model=None, event_sink: Callable[..., None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ):
        self.order = order
        self.config = dict(config)
        self.tool_client = tool_client
        self._event_sink = event_sink
        self._cancel_requested = cancel_requested or (lambda: False)
        self.model = model if model is not None else create_model(self.config)
        self.model_name = os.getenv("PRODUCE_AGENT_MODEL", self.config["model"])

    def _event(self, stage: str, **details) -> None:
        if self._event_sink is not None:
            self._event_sink(self.order.order_id, stage, **details)

    def _check_cancel(self) -> None:
        if self._cancel_requested():
            raise OrderCancelled("Cancellation accepted before the next Agent decision")

    def _invoke_tool(self, name: str, arguments: dict) -> dict:
        return self.tool_client.call(name, arguments)

    def _remaining(self) -> dict[str, int]:
        completed = self.tool_client.context()["completed"]
        return {
            line.sku: line.quantity - completed.get(line.sku, 0)
            for line in self.order.lines
        }

    def _next_sku(self) -> str | None:
        return next((sku for sku, count in self._remaining().items() if count), None)

    def run(self) -> None:
        """Run decisions using an already connected client; host owns its lifetime."""
        self._check_cancel()
        self._run_loop()

    def plan_order(self, context: dict) -> dict:
        """Ask for a bounded declarative plan, then reject any contract violation."""
        self._check_cancel()
        remaining = context["remaining"]
        digest = context_digest(context)
        allowed = [sku for sku, count in remaining.items() if count > 0]
        if not allowed:
            plan = {"version": 1, "context_digest": digest, "items": []}
            return validate_plan(plan, context)
        tool = {"type": "function", "function": {
            "name": "propose_fulfillment_plan",
            "description": "Choose an item order and bounded perception attempts; never include motion, coordinates or XML.",
            "parameters": {"type": "object", "properties": {
                "version": {"type": "integer", "enum": [1]},
                "context_digest": {"type": "string", "enum": [digest]},
                "items": {"type": "array", "minItems": sum(remaining.values()),
                          "maxItems": sum(remaining.values()), "items": {
                              "type": "object", "properties": {
                                  "sku": {"type": "string", "enum": allowed},
                                  "visual_attempts": {"type": "integer", "minimum": 1,
                                      "maximum": context["limits"]["max_visual_attempts_per_item"]}},
                              "required": ["sku", "visual_attempts"], "additionalProperties": False}}},
            "required": ["version", "context_digest", "items"], "additionalProperties": False}}}
        messages = [{"role": "system", "content":
            "Plan this admitted produce order. Include every remaining item exactly once. "
            "Inventory counts are uncertain observations, not proof of stock. "
            "Prefer a sensible SKU sequence using the supplied observations; do not invent SKUs or claim completion. "
            "Only propose the allowed tool arguments; execution will re-perceive and verify every item."},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        reply = self.model.chat.completions.create(model=self.model_name, messages=messages,
            tools=[tool], tool_choice={"type": "function", "function": {"name": "propose_fulfillment_plan"}},
            temperature=0)
        self._check_cancel()
        calls = reply.choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != "propose_fulfillment_plan":
            raise OrderError("Agent did not return exactly one plan proposal")
        try:
            proposal = json.loads(calls[0].function.arguments)
        except (ValueError, TypeError) as exc:
            raise OrderError("Agent plan is not valid JSON") from exc
        plan = validate_plan(proposal, context)
        self._event("AGENT_PLAN_PROPOSED", context_digest=digest,
                    items=plan["items"])
        return plan

    def select_candidate(self, target_sku: str | None = None) -> str:
        """BT owns execution; this Agent selects one actually perceived ID only."""
        self._check_cancel()
        sku = target_sku or self._next_sku()
        if sku is None:
            return ""
        if sku not in self._remaining() or self._remaining()[sku] < 1:
            raise OrderError("Requested selection SKU is not remaining in this order")
        result = self._invoke_tool("get_object_positions", {"sku": sku})
        detections = result.get("detections", [])
        self._event("AGENT_TOOL_RESULT", tool="get_object_positions", result=result)
        if not detections:
            return ""
        identifiers = [row["detection_id"] for row in detections]
        tool = {"type": "function", "function": {
            "name": "select_candidate", "description": "Select one observed candidate; this does not move the arm.",
            "parameters": {"type": "object", "properties": {
                "detection_id": {"type": "string", "enum": identifiers}},
                "required": ["detection_id"], "additionalProperties": False}}}
        messages = [{"role": "system", "content":
            "Choose one returned visual detection for the current requested SKU. Never invent IDs. "
            "The behavior tree, not your text, will execute and verify the operation."},
            {"role": "user", "content": json.dumps({"sku": sku, "remaining": self._remaining(),
                "detections": detections}, ensure_ascii=False)}]
        for _ in range(3):
            self._check_cancel()
            reply = self.model.chat.completions.create(model=self.model_name, messages=messages,
                tools=[tool], tool_choice={"type": "function", "function": {"name": "select_candidate"}}, temperature=0)
            self._check_cancel()
            calls = reply.choices[0].message.tool_calls or []
            if len(calls) == 1 and calls[0].function.name == "select_candidate":
                try:
                    args = json.loads(calls[0].function.arguments)
                    if set(args) == {"detection_id"} and args["detection_id"] in identifiers:
                        self._event("AGENT_CANDIDATE_SELECTED", sku=sku, detection_id=args["detection_id"])
                        return args["detection_id"]
                except (ValueError, TypeError):
                    pass
            messages.append({"role": "user", "content": "Return exactly one select_candidate call with an observed ID."})
        raise OrderError("Agent candidate selection exceeded bounded attempts")

    def _run_loop(self) -> None:
        goal = {line.sku: line.quantity for line in self.order.lines}
        context = self.tool_client.context()
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a produce order fulfillment robot agent. For a requested SKU, "
                    "first call get_object_positions to recognize visible items and "
                    "calculate their 3D positions from the RGB-D camera. Then choose "
                    "one returned detection_id for pick_and_place. Re-perceive after "
                    "every successful pick because coordinates become stale. If a "
                    "candidate fails the visual safety check, follow required_tool "
                    "and alternative_detection_ids to choose a different candidate "
                    "or perceive again. A rejected or stale detection is not a completed pick. "
                    "Complete each requested SKU in order before switching to the next. "
                    "Fulfil all requested counts, then call finish_order. "
                    "Never invent IDs or coordinates. Call one tool at a time."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"goal": "Collect the requested produce from mixed table stock into workstation-assigned bins", "mode": context["mode"], "zone": self.order.zone, "requested": goal,
                     "completed": context["completed"], "remaining": self._remaining()},
                    ensure_ascii=False,
                ),
            },
        ]
        perceived_sku = None
        empty_perceptions = 0
        candidate_rejections = 0
        max_calls = self.config["max_tool_calls_per_item"] * self.order.total_items + 6
        for _ in range(max_calls):
            self._check_cancel()
            response = self.model.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=self.tool_client.tools,
                tool_choice="auto",
                temperature=0,
            )
            self._check_cancel()
            message = response.choices[0].message
            calls = message.tool_calls or []
            if len(calls) != 1:
                self._event(
                    "AGENT_RETRY",
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
            self._event(
                "AGENT_TOOL_CALL", tool=name, arguments=arguments
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
                    candidate_rejections += 1
                    if candidate_rejections >= self.config["max_consecutive_empty_perceptions"]:
                        raise OrderError(
                            f"Candidate validation stopped after {candidate_rejections} rejections; "
                            f"last reason {exc.code}: {exc}. Inspect candidate diagnostics before retrying"
                        ) from exc
                    remaining_ids = self.tool_client.context()["candidate_ids"] if not exc.reobserve else []
                    if not remaining_ids:
                        perceived_sku = None
                    result = {"ok": False, "error": str(exc), "retryable": True,
                              "reason_code": exc.code,
                              "required_tool": "pick_and_place" if remaining_ids else "get_object_positions",
                              "alternative_detection_ids": remaining_ids, "required_sku": required_sku}
            self._event(
                "AGENT_TOOL_RESULT", tool=name, result=result
            )
            if name == "get_object_positions" and "detections" in result:
                if result["detections"]:
                    perceived_sku = arguments["sku"]
                    empty_perceptions = 0
                else:
                    perceived_sku = None
                    empty_perceptions += 1
                    limit = self.config["max_consecutive_empty_perceptions"]
                    self._event(
                        "PERCEPTION_EMPTY",
                        sku=arguments["sku"], attempts=empty_perceptions,
                        limit=limit, remaining=self._remaining(),
                    )
                    if empty_perceptions >= limit:
                        self._event(
                            "PERCEPTION_EXHAUSTED",
                            sku=arguments["sku"], attempts=empty_perceptions,
                            remaining=self._remaining(),
                        )
                        if result.get("raw_position_count", 0):
                            raise OrderError(
                                f"No selectable {arguments['sku']} candidate after {limit} consecutive "
                                "visual attempts; localized candidates were filtered by safety or reservation rules"
                            )
                        raise OrderError(
                            f"No {arguments['sku']} detected after {limit} consecutive "
                            f"visual attempts; {self._remaining()[arguments['sku']]} still needed"
                        )
            elif name == "pick_and_place" and result.get("placed"):
                perceived_sku = None
                empty_perceptions = 0
                candidate_rejections = 0
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
