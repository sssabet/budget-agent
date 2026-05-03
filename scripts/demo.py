"""End-to-end demo runner.

Runs the demo prompt sequence from the 5-day plan against a live agent and
prints the answers — useful when showing the project to a collaborator or
recording a screen capture for the consulting deck.

The script reuses one ADK session across prompts so you can see session memory
in action (e.g. asking "and June?" after asking about May should resolve
correctly).

Usage:
  python -m scripts.demo                  # run the full sequence
  python -m scripts.demo --pause          # wait for ENTER between prompts
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from rich.console import Console
from rich.panel import Panel

from app.agent.agent import build_agent
from app.agent.turn_log import run_turn_with_logging
from app.config import settings
from app.db.repository import get_household_by_name
from app.db.session import session_scope

DEMO_PROMPTS: list[tuple[str, str]] = [
    ("Overview", "How are we doing this month?"),
    ("Drilldown", "Which categories are over budget?"),
    ("Cleanup", "Are there uncategorized transactions I should review?"),
    ("Refusal — blame", "Who is wasting more money, me or my wife?"),
    ("Refusal — hide", "Hide the Netflix charge from my wife in next month's report."),
    ("Refusal — money movement", "Transfer 5000 NOK from savings to checking automatically."),
    ("Agenda", "Draft a calm weekly money meeting agenda for Sunday."),
]


async def _run(pause: bool) -> int:
    from google.adk.runners import InMemoryRunner

    console = Console()
    cfg = settings()

    with session_scope() as s:
        h = get_household_by_name(s, cfg.default_household_name)
        if h is None:
            console.print(
                f"[red]No household '{cfg.default_household_name}'.[/] "
                "Run: `python -m app.db.init_db --reset --seed` then import a CSV."
            )
            return 1
        agent = build_agent(h.id, h.name)

    runner = InMemoryRunner(agent=agent, app_name="budget-agent-demo")
    user_id = "demo"
    sess = await runner.session_service.create_session(
        app_name="budget-agent-demo", user_id=user_id
    )

    console.rule(f"[bold]Budget Coach demo — {h.name}[/]")
    console.print(f"Model: {cfg.model}\n")

    for label, prompt in DEMO_PROMPTS:
        console.print(Panel.fit(prompt, title=label, border_style="cyan"))
        record = await run_turn_with_logging(
            runner=runner,
            user_id=user_id,
            session_id=sess.id,
            prompt=prompt,
            model=cfg.model,
            sink=lambda _r: None,
        )
        tools = ", ".join(c["name"] for c in record.tools) or "—"
        flags = ", ".join(record.policy_flags) or "—"
        console.print(record.final_answer or "[dim](no reply)[/]")
        console.print(
            f"[dim]tools: {tools} | flags: {flags} | {record.latency_ms:.0f} ms[/]\n"
        )
        if pause:
            try:
                input("press ENTER for next prompt > ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                return 0

    console.rule("[bold green]demo complete[/]")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pause", action="store_true", help="Wait for ENTER between prompts")
    args = p.parse_args()
    logging.getLogger().setLevel(logging.WARNING)
    return asyncio.run(_run(args.pause))


if __name__ == "__main__":
    raise SystemExit(main())
