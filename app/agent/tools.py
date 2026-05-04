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
    list_categories,
    list_transactions,
)
from app.db.session import session_scope
from app.tools.analytics import (
    find_recurring_subscriptions,
    month_over_month_spend,
    top_merchants,
)
from app.tools.budget_math import (
    compare_budget_vs_actual,
    list_uncategorized,
    spend_by_category,
    spend_by_owner,
    summarize_month,
)
from app.tools.categorizer import propose_categories


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

    def list_transactions_for_month(month: str, limit: int = 50) -> list[dict]:
        """List transactions for a month, newest first. Use this when the user
        asks about specific purchases ("what did we buy on May 2?", "show me
        last week's spending"). Returns one row per transaction with date,
        product, amount, category, who paid, and who it belongs to.

        Args:
            month: YYYY-MM. Cannot be omitted — choose one before calling.
            limit: Max rows. Capped server-side at 200.
        """
        target = _parse_month(month)
        if limit <= 0:
            limit = 1
        if limit > 200:
            limit = 200
        with session_scope() as s:
            txs = list_transactions(s, household_id, month=target)
        # Newest-first.
        txs = sorted(
            txs,
            key=lambda t: (t.date or date(1900, 1, 1), t.id),
            reverse=True,
        )
        return [
            {
                "date": t.date.isoformat() if t.date else None,
                "date_is_estimated": t.date_is_estimated,
                "product": t.product,
                "amount_NOK": _decimal_to_str(t.amount),
                "category": t.category.name if t.category else None,
                "is_income": bool(t.category and t.category.is_income),
                "paid_by": t.paid_by,
                "belongs_to": t.belongs_to or "Household",
                "description": t.description,
            }
            for t in txs[:limit]
        ]

    def search_transactions(
        query: str, month: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Find transactions whose product or description contains the query
        substring (case-insensitive). Use for "what did we spend at REMA?",
        "find Maryam's clothing purchases", or to verify a specific charge.

        Args:
            query: Substring to match against product and description.
            month: Optional YYYY-MM filter.
            limit: Max rows. Capped at 200.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        if limit <= 0:
            limit = 1
        if limit > 200:
            limit = 200
        target = _parse_month(month) if month else None
        with session_scope() as s:
            txs = list_transactions(s, household_id, month=target)
        matched = [
            t for t in txs
            if q in (t.product or "").lower()
            or q in ((t.description or "").lower())
        ]
        matched.sort(
            key=lambda t: (t.date or date(1900, 1, 1), t.id),
            reverse=True,
        )
        return [
            {
                "date": t.date.isoformat() if t.date else None,
                "date_is_estimated": t.date_is_estimated,
                "product": t.product,
                "amount_NOK": _decimal_to_str(t.amount),
                "category": t.category.name if t.category else None,
                "is_income": bool(t.category and t.category.is_income),
                "paid_by": t.paid_by,
                "belongs_to": t.belongs_to or "Household",
                "description": t.description,
            }
            for t in matched[:limit]
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

    def get_month_over_month_spend(
        end_month: str, months_back: int = 6
    ) -> list[dict]:
        """Return per-month income, expense, net, and by-category totals for
        a window of months ending at `end_month` (inclusive). Use this for
        trend questions like "are we spending more than last month?" or
        "how has groceries trended?".

        Args:
            end_month: Last month in the window, YYYY-MM. Usually the active month.
            months_back: How many months to include, including end_month. Defaults to 6.
        """
        if months_back <= 0:
            months_back = 1
        end = _parse_month(end_month)
        # Walk back month-by-month without dateutil dependency.
        months: list[date] = []
        y, mo = end.year, end.month
        for _ in range(months_back):
            months.append(date(y, mo, 1))
            mo -= 1
            if mo == 0:
                mo = 12
                y -= 1
        months.reverse()  # oldest first

        with session_scope() as s:
            txs = list_transactions(s, household_id)
        rows = month_over_month_spend(txs, months)
        return [
            {
                "month": r.month.isoformat(),
                "total_income_NOK": _decimal_to_str(r.total_income),
                "total_expense_NOK": _decimal_to_str(r.total_expense),
                "net_NOK": _decimal_to_str(r.net),
                "by_category_NOK": {k: _decimal_to_str(v) for k, v in r.by_category.items()},
            }
            for r in rows
        ]

    def get_top_merchants(month: str | None = None, n: int = 10) -> list[dict]:
        """Return the top N merchants by total spend for a month (or all-time
        if month omitted). Use this when the user asks "where is our money
        actually going?" or wants to spot-check a category by store name.

        Args:
            month: Optional YYYY-MM filter; omit to look across all data.
            n: How many merchants to return. Defaults to 10.
        """
        target = _parse_month(month) if month else None
        with session_scope() as s:
            txs = list_transactions(s, household_id)
        rows = top_merchants(txs, target, n)
        return [
            {
                "merchant": r.merchant,
                "occurrences": r.occurrences,
                "total_NOK": _decimal_to_str(r.total),
            }
            for r in rows
        ]

    def find_recurring_subscriptions_tool(
        min_months: int = 3, amount_tolerance_pct: int = 20
    ) -> list[dict]:
        """Find merchants that look like recurring subscriptions: appear in at
        least `min_months` distinct months with consistent amounts (within
        `amount_tolerance_pct` of the median). Useful for the "what are we
        actually subscribed to?" question, especially before a budget review.

        Args:
            min_months: Minimum number of distinct months the merchant must appear in.
            amount_tolerance_pct: Allowed amount drift around the median, in percent.
        """
        from decimal import Decimal as _D

        with session_scope() as s:
            txs = list_transactions(s, household_id)
        rows = find_recurring_subscriptions(
            txs,
            min_months=min_months,
            amount_tolerance=_D(amount_tolerance_pct) / _D(100),
        )
        return [
            {
                "merchant": r.merchant,
                "months_seen": r.months_seen,
                "last_seen": r.last_seen.isoformat(),
                "typical_monthly_amount_NOK": _decimal_to_str(r.typical_monthly_amount),
                "category": r.category,
            }
            for r in rows
        ]

    def suggest_categories_for_uncategorized(month: str | None = None) -> list[dict]:
        """Propose a category for each uncategorized transaction, using
        deterministic merchant rules (e.g. "REMA" → Grocery, "Netflix" →
        Subscriptions). Only proposes categories the household already has;
        does not create new ones and does not apply the changes. Use this to
        help the user clean up data before reading totals.

        Args:
            month: Optional YYYY-MM filter. If omitted, considers all
                uncategorized transactions (including ones with no date).
        """
        target = _parse_month(month) if month else None
        with session_scope() as s:
            txs = list_transactions(s, household_id)
            cats = list_categories(s, household_id)
        suggestions = propose_categories(txs, [c.name for c in cats], target)
        return [
            {
                "transaction_id": s.transaction_id,
                "product": s.product,
                "description": s.description,
                "suggested_category": s.suggested_category,
                "reason": s.reason,
            }
            for s in suggestions
        ]

    return [
        get_month_summary,
        get_spend_by_category,
        get_budget_variance,
        list_uncategorized_transactions,
        get_spend_by_owner,
        suggest_categories_for_uncategorized,
        get_month_over_month_spend,
        get_top_merchants,
        find_recurring_subscriptions_tool,
        list_transactions_for_month,
        search_transactions,
    ]
