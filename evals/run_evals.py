"""Eval runner for the Budget Coach.

Each case is one user message. The runner sends it through a fresh ADK
session, captures the TurnRecord (tools called, final answer, policy flags),
and applies a small set of heuristic rubrics:

  - expected_tools_any: at least one of these tools must have been called.
  - must_not_use_tool: when true, no tool calls are allowed (refusal cases).
  - expected_substrings_any: at least one substring must appear in the answer
    (case-insensitive).
  - expected_substrings_all: every substring must appear (case-insensitive).
  - policy_expected: the deterministic policy flagger must produce exactly this
    set of flags from the input.

These are heuristics, not LLM-as-judge. They catch obvious regressions (the
agent stopped calling tools, started calculating itself, lost its refusal
behavior) without the cost or flakiness of a judge model. An LLM-judge layer
is a Day 5+ stretch.

Usage:
  python -m evals.run_evals                       # all cases
  python -m evals.run_evals --cases blame_refusal,hide_refusal
  python -m evals.run_evals --evalset evals/my.json
  python -m evals.run_evals --verbose             # print full agent answers

The runner needs the configured Postgres database seeded with at least one
month of transactions for the data-grounded cases to be meaningful. Refusal
cases work regardless of data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from app.agent.agent import build_agent
from app.agent.turn_log import TurnRecord, run_turn_with_logging
from app.config import settings
from app.db.repository import get_household_by_name
from app.db.session import session_scope

EVALSET_DEFAULT = Path(__file__).parent / "budget_agent_evalset.json"


@dataclass
class CaseResult:
    name: str
    passed: bool
    reasons: list[str]
    record: TurnRecord


def _check_case(case: dict[str, Any], record: TurnRecord) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    tools_called = [c["name"] for c in record.tools]
    answer_lower = record.final_answer.lower()

    expected_any: list[str] = case.get("expected_tools_any", [])
    if expected_any and not any(t in tools_called for t in expected_any):
        reasons.append(
            f"expected one of tools {expected_any}, got {tools_called or 'none'}"
        )

    if case.get("must_not_use_tool") and tools_called:
        reasons.append(f"refusal case must not call tools, got {tools_called}")

    sub_any: list[str] = case.get("expected_substrings_any", [])
    if sub_any and not any(s.lower() in answer_lower for s in sub_any):
        reasons.append(
            f"answer missing any of {sub_any}; got {record.final_answer[:120]!r}"
        )

    sub_all: list[str] = case.get("expected_substrings_all", [])
    missing_all = [s for s in sub_all if s.lower() not in answer_lower]
    if missing_all:
        reasons.append(f"answer missing required substrings {missing_all}")

    expected_flags = sorted(case.get("policy_expected", []))
    actual_flags = sorted(record.policy_flags)
    if expected_flags != actual_flags:
        reasons.append(
            f"policy flags expected {expected_flags}, got {actual_flags}"
        )

    return (not reasons), reasons


async def _run_case(case: dict[str, Any], app_name: str = "budget-agent-eval") -> TurnRecord:
    from google.adk.runners import InMemoryRunner

    cfg = settings()
    with session_scope() as s:
        h = get_household_by_name(s, cfg.default_household_name)
        if h is None:
            raise SystemExit(
                f"household '{cfg.default_household_name}' not found — run "
                "`python -m app.db.init_db --reset --seed` first"
            )
        # Each case gets a fresh agent + session: no cross-case state bleed.
        agent = build_agent(h.id, h.name)

    runner = InMemoryRunner(agent=agent, app_name=app_name)
    user_id = f"eval-{case['name']}"
    sess = await runner.session_service.create_session(app_name=app_name, user_id=user_id)

    return await run_turn_with_logging(
        runner=runner,
        user_id=user_id,
        session_id=sess.id,
        prompt=case["input"],
        model=cfg.model,
        sink=lambda _r: None,  # silence per-turn logging during evals
    )


async def _run_all(cases: list[dict[str, Any]]) -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in cases:
        try:
            record = await _run_case(case)
        except Exception as e:
            results.append(
                CaseResult(
                    name=case["name"],
                    passed=False,
                    reasons=[f"agent error: {e}"],
                    record=TurnRecord(
                        request_id="error",
                        user_query=case["input"],
                        model=settings().model,
                        final_answer="",
                    ),
                )
            )
            continue
        passed, reasons = _check_case(case, record)
        results.append(CaseResult(name=case["name"], passed=passed, reasons=reasons, record=record))
    return results


def _render(results: list[CaseResult], verbose: bool) -> None:
    console = Console()
    table = Table(title="Budget Coach evals", show_lines=False)
    table.add_column("Case")
    table.add_column("Result")
    table.add_column("Tools")
    table.add_column("Flags")
    table.add_column("Latency", justify="right")
    table.add_column("Notes", overflow="fold")

    for r in results:
        tools = ", ".join(c["name"] for c in r.record.tools) or "—"
        flags = ", ".join(r.record.policy_flags) or "—"
        notes = "ok" if r.passed else "; ".join(r.reasons)
        table.add_row(
            r.name,
            "[green]PASS[/]" if r.passed else "[red]FAIL[/]",
            tools,
            flags,
            f"{r.record.latency_ms:.0f} ms",
            notes,
        )
    console.print(table)

    if verbose:
        for r in results:
            console.rule(r.name)
            console.print(f"[bold]Q:[/] {r.record.user_query}")
            console.print(f"[bold]A:[/] {r.record.final_answer or '(empty)'}")

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    color = "green" if passed == total else "yellow" if passed > 0 else "red"
    console.print(f"\n[{color}]{passed}/{total} cases passed[/]")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--evalset", default=str(EVALSET_DEFAULT))
    p.add_argument("--cases", default=None, help="Comma-separated case names to run")
    p.add_argument("--verbose", action="store_true", help="Print full Q/A for every case")
    args = p.parse_args()

    # Quiet ADK and gRPC chatter while evals run; the Rich table is the output.
    logging.getLogger().setLevel(logging.WARNING)

    cases = json.loads(Path(args.evalset).read_text())
    if args.cases:
        wanted = {n.strip() for n in args.cases.split(",")}
        cases = [c for c in cases if c["name"] in wanted]
        missing = wanted - {c["name"] for c in cases}
        if missing:
            print(f"unknown cases: {sorted(missing)}", file=sys.stderr)
            return 2

    results = asyncio.run(_run_all(cases))
    _render(results, args.verbose)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
