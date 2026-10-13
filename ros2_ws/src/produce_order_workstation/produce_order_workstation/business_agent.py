"""Business decisions live in the Agent process; all business facts use MCP."""
import json
import os
from .order import OrderCancelled, OrderError


def run_business_agent(question, client, model, config, cancelled=lambda: False):
    if cancelled():
        raise OrderCancelled("Business request cancelled before querying facts")
    context = client.call("explain_task_status", {})
    messages = [{"role": "system", "content":
        "You are the produce workcell business assistant. Query tools before stating facts. "
        "Always distinguish workcell readiness from an empty order queue. INITIALIZING is not READY. "
        "COLLECTED means the operator ALREADY picked up the order and its bin is free. "
        "Only WAITING_PICKUP means awaiting pickup. Use state_label to interpret order states. "
        "Empty observations or UNKNOWN stock mean not confirmed, not zero stock or healthy inventory. "
        "Visual inventory is uncertain, not proof of shortage. Propose changes but never approve them. "
        "Tool requests do not prove replenishment or delivery. Explain permitted next steps in concise Chinese."},
        {"role": "user", "content": json.dumps({"question": question, "workcell": context}, ensure_ascii=False)}]
    trace = [{"tool": "explain_task_status", "result": context}]
    allowed = {tool["function"]["name"] for tool in client.tools}
    for _ in range(8):
        if cancelled():
            raise OrderCancelled("Business request cancelled")
        reply = model.chat.completions.create(model=os.getenv("PRODUCE_AGENT_MODEL", config["model"]),
            messages=messages, tools=client.tools, temperature=0)
        if cancelled():
            raise OrderCancelled("Business request cancelled after model reply")
        msg = reply.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            if trace:
                return {"answer": msg.content or "", "tools": trace, "observed_context": context}
            messages.append({"role": "user", "content": "请先查询真实状态，不要凭空回答。"})
            continue
        if len(trace) + len(calls) > 12:
            raise OrderError("Business tool budget exceeded")
        messages.append({"role": "assistant", "content": msg.content,
            "tool_calls": [{"id": call.id, "type": "function", "function": {
                "name": call.function.name, "arguments": call.function.arguments}} for call in calls]})
        for call in calls:
            try:
                if call.function.name not in allowed:
                    raise ValueError("Business tool not authorized")
                value = client.call(call.function.name, json.loads(call.function.arguments))
            except (ValueError, OrderError) as exc:
                value = {"error": str(exc)}
            trace.append({"tool": call.function.name, "result": value})
            messages.append({"role": "tool", "tool_call_id": call.id,
                "content": json.dumps(value, ensure_ascii=False)})
    raise OrderError("Business Agent reached bounded decision budget")
