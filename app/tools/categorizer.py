"""Deterministic merchant → category rules.

This is the cheapest way to clean up uncategorized transactions: a substring
match on the product name. Norwegian-first because that's the household's
reality. An LLM-assisted fallback for unknown merchants is a later slice — for
now the agent's job is to *propose*, not auto-apply, so a partial set of rules
is fine.

Match rules:
- Case-insensitive substring match against `product` (and `description` as
  fallback).
- First matching rule wins, in insertion order. Put more specific rules above
  more generic ones (e.g. "circle k" before "k" — though we wouldn't add "k").
- If the suggested category isn't one of the household's existing categories,
  skip the suggestion. We don't invent categories here; that's a human
  decision and lives in the planning page.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from app.tools.types import Transaction

# Token (lowercase substring) -> canonical category name. The lookup against the
# household's real category names is case-insensitive, so casing here is for
# readability only.
MERCHANT_RULES: dict[str, str] = {
    # Groceries
    "rema": "Groceries",
    "kiwi": "Groceries",
    "coop extra": "Groceries",
    "meny": "Groceries",
    "bunnpris": "Groceries",
    "joker": "Groceries",
    "europris": "Groceries",
    # Subscriptions / Entertainment
    "netflix": "Subscriptions & Entertainment",
    "spotify": "Subscriptions & Entertainment",
    "hbo": "Subscriptions & Entertainment",
    "viaplay": "Subscriptions & Entertainment",
    "youtube premium": "Subscriptions & Entertainment",
    "disney+": "Subscriptions & Entertainment",
    "apple.com/bill": "Subscriptions & Entertainment",
    # Transport (public)
    "ruter": "Transport",
    "vy": "Transport",
    "flytoget": "Transport",
    "nsb": "Transport",
    # Car / fuel
    "circle k": "Car",
    "shell": "Car",
    "esso": "Car",
    "uno-x": "Car",
    "yx": "Car",
    # Utilities
    "telenor": "Utilities",
    "telia": "Utilities",
    "ice": "Utilities",
    "fjordkraft": "Utilities",
    "tibber": "Utilities",
    "hafslund": "Utilities",
    "fortum": "Utilities",
    # Health & wellness
    "apotek": "Health & Wellness",
    "vitusapotek": "Health & Wellness",
    "boots": "Health & Wellness",
    "sats": "Health & Wellness",
    "elixia": "Health & Wellness",
    # Eating out
    "mcdonald": "Eating Out",
    "burger king": "Eating Out",
    "starbucks": "Eating Out",
    "espresso house": "Eating Out",
    "kaffebrenneriet": "Eating Out",
    "foodora": "Eating Out",
    "wolt": "Eating Out",
}


@dataclass(frozen=True)
class CategorySuggestion:
    transaction_id: str
    product: str
    description: str | None
    suggested_category: str  # the household's actual category name (preserves casing)
    reason: str  # human-readable, e.g. "matched 'rema' in product"


def _searchable(t: Transaction) -> str:
    parts = [t.product or ""]
    if t.description:
        parts.append(t.description)
    return " ".join(parts).lower()


def _resolve_household_category(
    canonical: str, available_by_lower: dict[str, str]
) -> str | None:
    return available_by_lower.get(canonical.lower())


def propose_for_transaction(
    t: Transaction,
    available_by_lower: dict[str, str],
) -> CategorySuggestion | None:
    """Returns a single suggestion or None. Skips already-categorized rows."""
    if t.category is not None:
        return None
    text = _searchable(t)
    if not text.strip():
        return None
    for token, canonical in MERCHANT_RULES.items():
        if token in text:
            resolved = _resolve_household_category(canonical, available_by_lower)
            if resolved is None:
                continue  # rule fired but household doesn't have that category
            return CategorySuggestion(
                transaction_id=t.id,
                product=t.product,
                description=t.description,
                suggested_category=resolved,
                reason=f"matched '{token}' in product/description",
            )
    return None


def propose_categories(
    transactions: Iterable[Transaction],
    available_categories: Iterable[str],
    month: date | None = None,
) -> list[CategorySuggestion]:
    """Return suggestions for uncategorized transactions, optionally restricted
    to a month. Transactions with no `date` are included only when `month` is
    None — we won't pretend to know which month they belong to."""
    available_by_lower = {name.lower(): name for name in available_categories}
    suggestions: list[CategorySuggestion] = []
    for t in transactions:
        if t.category is not None:
            continue
        if month is not None:
            if t.date is None:
                continue
            if t.date.replace(day=1) != month.replace(day=1):
                continue
        s = propose_for_transaction(t, available_by_lower)
        if s is not None:
            suggestions.append(s)
    return suggestions
