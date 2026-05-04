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
import logging
import sys

from app.agent.agent import build_agent
from app.agent.turn_log import run_turn_with_logging
from app.config import settings
from app.db.repository import get_household_by_name
from app.db.session import session_scope

APP_NAME = "budget-agent"


def _setup_logging() -> None:
    # One JSON line per turn on stderr. Quiet enough that interactive output
    # stays readable, structured enough that it can be tail'd into a file.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("budget_agent.turn")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


async def _run_one(runner, user_id: str, session_id: str, prompt: str, model: str) -> str:
    record = await run_turn_with_logging(
        runner=runner,
        user_id=user_id,
        session_id=session_id,
        prompt=prompt,
        model=model,
    )
    # Surface tool calls visibly to the human as a brief summary.
    for call in record.tools:
        latency = f"{call['latency_ms']}ms" if call.get("latency_ms") is not None else "?"
        print(f"  [tool] {call['name']}({call['args']}) -> {latency}", file=sys.stderr)
    return record.final_answer


async def _amain(once: str | None) -> int:
    from google.adk.runners import InMemoryRunner

    _setup_logging()
    cfg = settings()
    with session_scope() as s:
        h = get_household_by_name(s, cfg.default_household_name)
        if h is None:
            print(f"household '{cfg.default_household_name}' not found", file=sys.stderr)
            return 1
        agent = build_agent(h.id, h.name)
        household_label = h.name

    runner = InMemoryRunner(agent=agent, app_name=APP_NAME)
    user_id = "cli"
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=user_id)

    if once is not None:
        text = await _run_one(runner, user_id, session.id, once, cfg.model)
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
        text = await _run_one(runner, user_id, session.id, prompt, cfg.model)
        print(f"agent> {text}\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", default=None, help="Send a single message and exit")
    args = p.parse_args()
    return asyncio.run(_amain(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
