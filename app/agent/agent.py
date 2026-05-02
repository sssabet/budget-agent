"""Agent factory.

Builds an ADK Agent bound to a specific household. The agent's tools close over
the household_id so the LLM never sees or has to pass it.

Two run modes are supported via env:
  - GOOGLE_GENAI_USE_VERTEXAI=true (default) -> Vertex AI on the user's GCP project.
    Requires `gcloud auth application-default login` and GOOGLE_CLOUD_PROJECT set.
  - GOOGLE_GENAI_USE_VERTEXAI=false                 -> AI Studio (needs GOOGLE_API_KEY).
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


def build_agent(household_id: uuid.UUID, household_name: str) -> Agent:
    """Build a Budget Coach agent bound to one household.

    The current date is injected into the instruction at construction time. Without
    this the model defaults to its training cutoff when interpreting "this month"
    or bare month names — e.g. "May" -> 2024-05 -> empty results.
    """
    cfg = settings()
    today = date.today()
    instruction = load_system_instruction()
    instruction += (
        f"\n\n# Context\n"
        f"Household name: **{household_name}**. Currency: NOK.\n"
        f"Today's date: **{today.isoformat()}**. "
        f"When the user says 'this month' or a bare month name, resolve relative to today. "
        f"Default to {today.strftime('%Y-%m')} unless they specify a different year.\n"
    )
    return Agent(
        name="budget_coach",
        model=cfg.model,
        description="Household budget coach that explains spending and uses deterministic tools for math.",
        instruction=instruction,
        tools=build_household_tools(household_id),
    )
