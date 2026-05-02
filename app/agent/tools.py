"""ADK tool functions.

These are plain Python functions with type hints and docstrings — ADK derives the
JSON schema from those. The agent decides which to call and what arguments to pass.
The functions never touch the LLM; they read from Postgres and return structured data.

How household_id flows: each tool function closes over a household_id captured by
build_household_tools(). That keeps tool signatures clean (the agent doesn't have
to pass an opaque UUID around) and means tool calls are scoped to one household,
which is what we want for multi-tenant safety.
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Callable

from app.db.repository import (
    categories_by_id,
    list_budgets,
    list_transactions,
)
from app.db.session import session_scope
from app.tools.budget_math import (
    compare_budget_vs_actual,
    list_uncategorized,
    spend_by_category,
    spend_by_owner,
    summarize_month,
)


def _parse_month(month: str) -> date:
    y, m = month.split("-")
    return date(int(y), int(m), 1)


def _decimal_to_str(x: Decimal) -> str:
    # JSON-serializable; rounding handled at display time.
    return f"{x:.2f}"


def build_household_tools(household_id: uuid.UUID) -> list[Callable[..., Any]]:
    """Return tool functions bound to a specific household."""

    def get_month_summary(month: str) -> dict:
        """Return a high-level summary for a given month: total income, total
        expense, net, by-category spend, over-budget categories, and counts of
        rows that need attention (uncategorized or month-only date precision).

        Args:
            month: Month in YYYY-MM format, e.g. "2026-05".
        """
        target = _parse_month(month)
        with session_scope() as s:
            txs = list_transactions(s, household_id)
            budgets = list_budgets(s, household_id)
            cats = categories_by_id(s, household_id)
        sm = summarize_month(txs, budgets, cats, target)
        return {
            "month": sm.month.isoformat(),
            "total_income_NOK": _decimal_to_str(sm.total_income),
            "total_expense_NOK": _decimal_to_str(sm.total_expense),
            "net_NOK": _decimal_to_str(sm.net),
            "by_category_NOK": {k: _decimal_to_str(v) for k, v in sm.by_category.items()},
            "over_budget": [
                {
                    "category": r.category_name,
                    "budgeted_NOK": _decimal_to_str(r.budgeted),
                    "actual_NOK": _decimal_to_str(r.actual),
                    "variance_NOK": _decimal_to_str(r.variance),
                }
                for r in sm.over_budget_categories
            ],
            "uncategorized_count": sm.uncategorized_count,
            "estimated_date_count": sm.estimated_date_count,
        }

    def get_spend_by_category(month: str) -> dict:
        """Return the total spend per expense category for a given month.

        Args:
            month: Month in YYYY-MM format, e.g. "2026-05".
        """
        target = _parse_month(month)
        with session_scope() as s:
            txs = list_transactions(s, household_id)
        result = spend_by_category(txs, target)
        return {k: _decimal_to_str(v) for k, v in result.items()}

    def get_budget_variance(month: str) -> list[dict]:
        """Return per-category budget vs actual spending with variance and a
        status of 'under', 'near' (>=85% of budget), or 'over'.

        Args:
            month: Month in YYYY-MM format, e.g. "2026-05".
        """
        target = _parse_month(month)
        with session_scope() as s:
            txs = list_transactions(s, household_id)
            budgets = list_budgets(s, household_id)
            cats = categories_by_id(s, household_id)
        reports = compare_budget_vs_actual(txs, budgets, cats, target)
        return [
            {
                "category": r.category_name,
                "budgeted_NOK": _decimal_to_str(r.budgeted),
                "actual_NOK": _decimal_to_str(r.actual),
                "variance_NOK": _decimal_to_str(r.variance),
                "status": r.status,
            }
            for r in reports
        ]

    def list_uncategorized_transactions(month: str | None = None) -> list[dict]:
        """List transactions missing a category. Useful when the user wants to
        clean up data or when the agent needs to flag items for review.

        Args:
            month: Optional YYYY-MM filter. If omitted, returns all uncategorized.
        """
        target = _parse_month(month) if month else None
        with session_scope() as s:
            txs = list_transactions(s, household_id)
        out = list_uncategorized(txs, target)
        return [
            {
                "id": t.id,
                "date": t.date.isoformat() if t.date else None,
                "date_is_estimated": t.date_is_estimated,
                "product": t.product,
                "amount_NOK": _decimal_to_str(t.amount),
                "paid_by": t.paid_by,
                "description": t.description,
            }
            for t in out
        ]

    def get_spend_by_owner(month: str) -> dict:
        """Return spending grouped by the person it 'belongs_to' (None -> 'Household').
        Use only when the user explicitly asks about per-person spend.

        Args:
            month: Month in YYYY-MM format, e.g. "2026-05".
        """
        target = _parse_month(month)
        with session_scope() as s:
            txs = list_transactions(s, household_id)
        result = spend_by_owner(txs, target)
        return {k: _decimal_to_str(v) for k, v in result.items()}

    return [
        get_month_summary,
        get_spend_by_category,
        get_budget_variance,
        list_uncategorized_transactions,
        get_spend_by_owner,
    ]
