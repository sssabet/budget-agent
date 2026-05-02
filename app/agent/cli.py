"""Interactive CLI for the budget agent.

Usage:
  # interactive REPL
  python -m app.agent.cli

  # one-shot
  python -m app.agent.cli --once "How are we doing in May?"
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from google.genai import types as genai_types

from app.agent.agent import build_agent
from app.config import settings
from app.db.repository import get_household_by_name
from app.db.session import session_scope

APP_NAME = "budget-agent"


async def _run_one(runner, user_id: str, session_id: str, prompt: str) -> str:
    msg = genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])
    final_text_parts: list[str] = []
    async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=msg):
        # Surface tool calls so the human can see what happened
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.function_call:
                    print(f"  [tool] {part.function_call.name}({dict(part.function_call.args)})", file=sys.stderr)
                if part.function_response:
                    print(f"  [tool result] {part.function_response.name} -> ok", file=sys.stderr)
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_text_parts.append(part.text)
    return "".join(final_text_parts).strip()


async def _amain(once: str | None) -> int:
    from google.adk.runners import InMemoryRunner

    cfg = settings()
    with session_scope() as s:
        h = get_household_by_name(s, cfg.dev_household_name)
        if h is None:
            print(f"household '{cfg.dev_household_name}' not found", file=sys.stderr)
            return 1
        agent = build_agent(h.id, h.name)
        household_label = h.name

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    user_id = "saeed-local"
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=user_id)

    if once is not None:
        text = await _run_one(runner, user_id, session.id, once)
        print(text)
        return 0

    print(f"Budget Coach for {household_label} — type 'exit' to quit")
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            return 0
        text = await _run_one(runner, user_id, session.id, prompt)
        print(f"agent> {text}\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", default=None, help="Send a single message and exit")
    args = p.parse_args()
    return asyncio.run(_amain(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
