"""Structured per-turn logging.

Wraps a single agent turn (one user message → final response) and emits one
JSON line capturing what happened. The plan calls for:
  request_id, user_query, tool_called, tool_latency_ms, model_name,
  final_answer, policy_flags

We extend that lightly: every tool call (not just one) is recorded, with its
arguments and per-call latency. Latency is total turn time; per-tool latency
is approximated as the gap between the function_call event and the matching
function_response event.

Output: one JSON line per turn, written to stderr by default. Override the
sink via the TurnLogger constructor when calling from a UI that wants in-app
display, or from the eval runner that wants to capture logs in memory.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, TextIO

from google.genai import types as genai_types

from app.agent.policy import flag_names

logger = logging.getLogger("budget_agent.turn")


@dataclass
class _ToolCall:
    name: str
    args: dict[str, Any]
    start_ms: float
    end_ms: float | None = None

    @property
    def latency_ms(self) -> float | None:
        if self.end_ms is None:
            return None
        return round(self.end_ms - self.start_ms, 2)


@dataclass
class TurnRecord:
    request_id: str
    user_query: str
    model: str
    final_answer: str
    tools: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    policy_flags: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "request_id": self.request_id,
                "user_query": self.user_query,
                "model": self.model,
                "tools": self.tools,
                "policy_flags": self.policy_flags,
                "final_answer_chars": len(self.final_answer),
                "latency_ms": round(self.latency_ms, 2),
            },
            ensure_ascii=False,
        )


def _now_ms() -> float:
    return time.perf_counter() * 1000


async def run_turn_with_logging(
    *,
    runner: Any,
    user_id: str,
    session_id: str,
    prompt: str,
    model: str,
    sink: Callable[[TurnRecord], None] | None = None,
) -> TurnRecord:
    """Send `prompt` through `runner` and return a TurnRecord describing it.

    The default sink writes one JSON line to stderr via the `budget_agent.turn`
    logger. The eval runner passes a custom sink that just collects records.
    """
    msg = genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])
    started = _now_ms()
    final_text_parts: list[str] = []
    tool_calls: list[_ToolCall] = []
    open_calls: dict[str, _ToolCall] = {}

    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=msg
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    call = _ToolCall(
                        name=part.function_call.name,
                        args=dict(part.function_call.args or {}),
                        start_ms=_now_ms(),
                    )
                    tool_calls.append(call)
                    # ADK doesn't always pair calls/responses by id, so we key
                    # by name and pop the most recent matching open call when
                    # the response arrives. Good enough for single-tool turns;
                    # fine for parallel as long as no two are in-flight with
                    # the same name (rare in this agent).
                    open_calls[call.name] = call
                if part.function_response:
                    name = part.function_response.name
                    call = open_calls.pop(name, None)
                    if call is not None:
                        call.end_ms = _now_ms()
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text_parts.append(part.text)

    finished = _now_ms()
    final = "".join(final_text_parts).strip()

    record = TurnRecord(
        request_id=uuid.uuid4().hex[:12],
        user_query=prompt,
        model=model,
        final_answer=final,
        tools=[
            {
                "name": c.name,
                "args": c.args,
                "latency_ms": c.latency_ms,
            }
            for c in tool_calls
        ],
        latency_ms=finished - started,
        policy_flags=flag_names(prompt),
    )

    if sink is not None:
        sink(record)
    else:
        logger.info(record.to_json())

    return record
