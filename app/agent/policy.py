"""Policy detector — flags user prompts that touch the household-safety rules.

The system instruction tells the LLM how to *refuse* these. This module's job
is *observation*: identify which policy-relevant pattern fired so we can log
it (for evaluation, debugging, and later for governance dashboards). It does
not block, rewrite, or short-circuit anything.

Why two layers? The LLM does the nuanced refusal text. The detector gives us
a stable, machine-readable signal — useful when you want to count "blame
prompts per week" or assert in evals that a blame prompt was indeed flagged.

Keep patterns conservative. False positives are noisy; false negatives are
caught by the LLM's own refusal logic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Each rule maps a flag name to one or more compiled patterns. Patterns match
# against the lowercased prompt. Use word boundaries where possible to avoid
# clobbering substrings (e.g. "blamange" would otherwise match "blame").
_RULES: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "blame",
        [
            re.compile(
                r"\b(who is wasting|wasting more|whose fault|blame|who spent more|"
                r"my (wife|husband|partner)'s fault)\b"
            ),
        ],
    ),
    (
        "hide",
        [
            # Allow a few words between "hide ... charge/purchase/etc"
            # so "hide this Netflix charge" matches as well as "hide this charge".
            re.compile(
                r"\bhide\b.{0,40}?\b(purchase|transaction|charge|expense|bill|amount|spending|payment)\b"
            ),
            re.compile(r"\bhide\b.{0,40}?\bfrom (my )?(wife|husband|partner)\b"),
            re.compile(r"\bdon'?t (tell|show) (my )?(wife|husband|partner)\b"),
            re.compile(r"\bkeep (this|it) (a )?secret\b"),
            re.compile(r"\bconceal\b"),
        ],
    ),
    (
        "money_movement",
        [
            # "transfer 5000 NOK", "move funds", "transfer to savings"
            re.compile(
                r"\b(transfer|move)\b.{0,40}?\b(money|funds|nok|kr|usd|eur|sek)\b"
            ),
            re.compile(
                r"\b(transfer|move)\b.{0,40}?\bto (checking|savings|account)\b"
            ),
            re.compile(r"\bpay (this|the bill|them|it now)\b"),
            re.compile(r"\bschedule (a )?payment\b"),
            re.compile(r"\bauto[- ]?pay\b"),
        ],
    ),
    (
        "individual_attribution_request",
        # Soft signal — not unsafe, but worth noting so we can verify the
        # agent confirmed before switching to per-person breakdowns.
        [re.compile(r"\b(per[- ]?person|by person|each of us|(my|her|his) own )\b")],
    ),
]


@dataclass(frozen=True)
class PolicyHit:
    flag: str
    matched_text: str


def flag_policy(query: str) -> list[PolicyHit]:
    """Return every rule that fires against the prompt. Empty list = clean.
    Multiple patterns under the same flag emit one hit per match."""
    if not query:
        return []
    haystack = query.lower()
    hits: list[PolicyHit] = []
    for flag, patterns in _RULES:
        for pattern in patterns:
            m = pattern.search(haystack)
            if m:
                hits.append(PolicyHit(flag=flag, matched_text=m.group(0)))
    return hits


def flag_names(query: str) -> list[str]:
    """Convenience: just the flag names, dedup-preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for h in flag_policy(query):
        if h.flag not in seen:
            seen.add(h.flag)
            out.append(h.flag)
    return out
