"""Agent factory.

Builds an ADK Agent bound to a specific household. The agent's tools close over
the household_id so the LLM never sees or has to pass it. Production uses Vertex
AI through the Cloud Run service account.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from google.adk.agents import Agent

from app.agent.tools import build_household_tools
from app.config import settings

PROMPT_PATH = Path(__file__).parent / "prompts" / "system_instruction.md"


def load_system_instruction() -> str:
    return PROMPT_PATH.read_text()


def build_agent(
    household_id: uuid.UUID,
    household_name: str,
    *,
    default_month: date | None = None,
) -> Agent:
    """Build a Budget Coach agent bound to one household.

    The current date is injected into the instruction at construction time. Without
    this the model defaults to its training cutoff when interpreting "this month"
    or bare month names — e.g. "May" -> 2024-05 -> empty results.

    If `default_month` is provided (typically the month the user has selected in the
    UI), the agent treats that as the active month when the user doesn't specify one.
    The user can still steer to a different month in conversation.
    """
    cfg = settings()
    today = date.today()
    active_month = (default_month or today).strftime("%Y-%m")
    instruction = load_system_instruction()
    instruction += (
        f"\n\n# Context\n"
        f"Household name: **{household_name}**. Currency: NOK.\n"
        f"Today's date: **{today.isoformat()}**. "
        f"Active month: **{active_month}** — use this when the user says "
        f"'this month' or asks a question without naming a month. "
        f"If they name a different month in conversation, switch to it for the rest "
        f"of the chat unless they switch again.\n"
    )
    return Agent(
        name="budget_coach",
        model=cfg.model,
        description="Household budget coach that explains spending and uses deterministic tools for math.",
        instruction=instruction,
        tools=build_household_tools(household_id),
    )
