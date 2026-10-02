"""Replay a trusted escalation tool result as an append-only Responses update."""
import json


def update_from_tool_result(message, model, *, tool_name=None):
    if message.get("role") != "tool" or (tool_name or message.get("name")) != "escalate":
        return None
    try:
        result = json.loads(message.get("content", ""))
        update = result.get("reasoning_update", {})
        target = str(model or "").split("/")[-1]
        if (target != "gpt-6-luna" or str(update.get("model", "")).split("/")[-1] != target
                or update.get("effort") != "high" or result.get("escalated") is not False):
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    return {"type": "configuration_update", "reasoning": {"effort": "high"}}


def reasoning_updates(messages, model):
    """Index updates by source row, accepting only results paired with the escalation tool."""
    calls = {}
    updates = {}
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                calls[call.get("id")] = (call.get("function") or {}).get("name")
        update = update_from_tool_result(message, model, tool_name=calls.get(message.get("tool_call_id"), "unknown"))
        if update:
            updates[index] = update
    return updates
