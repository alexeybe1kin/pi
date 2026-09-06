"""Turning a model's request to act into a ToolGate invocation.

Pi executes nothing itself. This module reads what the model asked for, hands it
to ToolGate, and hands the answer back - including "the owner has to confirm
this", which parks the turn rather than failing it.

The request format is a JSON object the model emits on its own line. Deliberately
not a provider's native tool-calling schema: Pi routes across local models, free
hosted models and paid ones, and their tool formats disagree. A format the loop
owns behaves identically everywhere, which is the whole reason the loop is thin.
Models that support native tool calls can be adapted onto this in #31 and later
without changing what ToolGate sees.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .toolgate import Tool

# A line that is exactly a JSON object with a "tool" key. Anchored and
# single-line on purpose: a looser parser would find "tool calls" inside prose
# the model wrote about tools, and act on a sentence.
_CALL = re.compile(r'^\s*(\{\s*"tool"\s*:.*\})\s*$', re.MULTILINE)


@dataclass(frozen=True)
class ToolCall:
    tool_id: str
    args: dict


def describe(tools: list[Tool]) -> str:
    """The system text that tells a model what it may ask for.

    Says plainly that some actions need the owner, because a model that thinks
    refusal is failure will retry or apologise instead of waiting.
    """
    if not tools:
        return ""
    lines = [
        ("You can ask to run a tool. To do so, reply with a single line that is "
         "exactly one JSON object and nothing else:"),
        '{"tool": "<id>", "args": {...}}',
        "",
        "Ask for one tool at a time, and wait for the result before asking again.",
        ("Some tools need the owner to confirm before they run. That is normal, not "
         "a failure: say what you are waiting for and stop."),
        "",
        "Available tools:",
    ]
    for tool in tools:
        fields = ", ".join(f"{f.get('name')}: {f.get('type', 'string')}" for f in tool.inputs)
        lines.append(f"- {tool.id} ({tool.name}): {tool.description or 'no description'}"
                     + (f" | args: {fields}" if fields else " | args: none"))
    return "\n".join(lines)


def parse(text: str, allowed: set[str]) -> ToolCall | None:
    """Find a tool request, or None.

    `allowed` is what ToolGate said this key is scoped to. A request for
    anything else is ignored here and never sent - ToolGate would refuse it
    anyway, but forwarding it would put an unscoped tool id in its audit trail
    on Pi's authority, which is not Pi's to spend.
    """
    for match in _CALL.finditer(text or ""):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        tool_id = payload.get("tool")
        if not isinstance(tool_id, str) or tool_id not in allowed:
            continue
        args = payload.get("args")
        return ToolCall(tool_id=tool_id, args=args if isinstance(args, dict) else {})
    return None
