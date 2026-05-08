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

import hashlib
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.db.repository import (
    categories_by_id,
    list_budgets,
    list_categories,
    list_transactions,
    upsert_budget,
)
from app.db.session import session_scope
from app.tools.analytics import (
    find_recurring_subscriptions,
    month_over_month_spend,
    top_merchants,
)
from app.tools.budget_math import (
    compare_budget_vs_actual,
    compute_planning_baseline,
    diff_budget,
    list_uncategorized,
    spend_by_category,
    spend_by_owner,
    suggest_allocations,
    summarize_month,
)
from app.tools.categorizer import propose_categories


def _parse_month(month: str) -> date:
    y, m = month.split("-")
    return date(int(y), int(m), 1)


def _decimal_to_str(x: Decimal) -> str:
    # JSON-serializable; rounding handled at display time.
    return f"{x:.2f}"


def _to_decimal(value: Any, *, field: str) -> Decimal:
    """Coerce an LLM-supplied number/string to Decimal, with a clear error.

    The agent will sometimes pass numbers as ints, floats, or strings depending
    on the path the model took. Anything that can't parse cleanly should raise
    rather than silently round wrong.
    """
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a number, got {value!r}") from exc


def _plan_token(
    month: date,
    allocations: dict[str, Decimal],
    savings_target: Decimal | None,
) -> str:
    """Stable hash over (month, allocations, savings_target).

    Why: `apply_budget_plan` re-derives this from its arguments and rejects the
    call if it doesn't match the token from `draft_budget_plan`. That stops the
    agent from applying a plan the user never confirmed (e.g. silently tweaked
    numbers between draft and apply).
    """
    pairs = sorted(
        (name.strip().lower(), f"{amount:.2f}") for name, amount in allocations.items()
    )
    saving = f"{savings_target:.2f}" if savings_target is not None else "-"
    canonical = (
        month.isoformat()
        + "|"
        + ";".join(f"{n}={a}" for n, a in pairs)
        + "|"
        + saving
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _current_budget_for_month(
    s, household_id: uuid.UUID, month: date
) -> dict[str, Decimal]:
    """{category_name: amount} for the household's existing budget for `month`."""
    cat_map = categories_by_id(s, household_id)  # id (str) -> name
    out: dict[str, Decimal] = {}
    for b in list_budgets(s, household_id, month=month):
        name = cat_map.get(b.category_id)
        if name is not None:
            out[name] = b.amount
    return out


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

    def get_planning_baseline(months_back: int = 6) -> dict:
        """Return rolling per-category statistics for the household over the
        last `months_back` months ending at the active month. Use this as the
        first step when the user wants help planning a budget — it gives you
        the numbers to ground the conversation in (avg, median, p90, recurring
        floor) instead of guessing.

        The window is zero-filled: a category seen in 2 of 6 months has its
        avg divided by 6, not 2 — that's the right baseline for "what should
        a typical month look like?".

        Args:
            months_back: How many months to include in the baseline window,
                ending at today's month inclusive. Defaults to 6.
        """
        if months_back <= 0:
            months_back = 1
        if months_back > 24:
            months_back = 24
        today = date.today()
        months: list[date] = []
        y, mo = today.year, today.month
        for _ in range(months_back):
            months.append(date(y, mo, 1))
            mo -= 1
            if mo == 0:
                mo = 12
                y -= 1
        months.reverse()

        with session_scope() as s:
            txs = list_transactions(s, household_id)
        baseline = compute_planning_baseline(txs, months)
        return {
            "window_months": [m.isoformat() for m in baseline.months],
            "avg_monthly_income_NOK": _decimal_to_str(baseline.avg_monthly_income),
            "avg_monthly_expense_NOK": _decimal_to_str(baseline.avg_monthly_expense),
            "avg_monthly_net_NOK": _decimal_to_str(baseline.avg_monthly_net),
            "categories": [
                {
                    "category": c.category_name,
                    "is_income": c.is_income,
                    "months_observed": c.months_observed,
                    "avg_monthly_NOK": _decimal_to_str(c.avg_monthly),
                    "median_monthly_NOK": _decimal_to_str(c.median_monthly),
                    "p90_monthly_NOK": _decimal_to_str(c.p90_monthly),
                    "last_month_NOK": _decimal_to_str(c.last_month),
                    "recurring_floor_NOK": _decimal_to_str(c.recurring_floor),
                }
                for c in baseline.categories
            ],
        }

    def draft_budget_plan(
        month: str,
        strategy: str,
        adjustments: dict[str, Any] | None = None,
        savings_target_NOK: str | None = None,
        months_back: int = 6,
    ) -> dict:
        """Draft a per-category budget proposal for `month` *without* writing
        anything. Returns the proposed allocations, a diff against the current
        budget, expected income/expense/net, a feasibility flag, and a
        `plan_token` the agent must echo back when calling `apply_budget_plan`.

        Always show the proposal to the user and ask for explicit confirmation
        before applying. If the user wants edits, redraft (do not edit the
        proposal locally).

        Args:
            month: Target month in YYYY-MM format.
            strategy: One of "keep" (copy current budget; fall back to last
                month's actual if no current budget), "rolling_average" (use
                avg monthly spend per category over the baseline window), or
                "adjust" (start from current budget and apply per-category
                deltas from `adjustments`).
            adjustments: For strategy="adjust", a mapping of category name ->
                signed delta in NOK (e.g. {"Groceries": "-1000", "Eating out":
                "500"}). Ignored for other strategies.
            savings_target_NOK: Optional savings goal for the month. If set,
                feasibility compares (income - expense) against this target.
            months_back: Baseline window size for rolling_average and stats.
                Defaults to 6.
        """
        target = _parse_month(month)
        if strategy not in {"keep", "rolling_average", "adjust"}:
            return {
                "ok": False,
                "error": f"unknown strategy {strategy!r}; choose keep, rolling_average, or adjust",
            }

        savings = (
            _to_decimal(savings_target_NOK, field="savings_target_NOK")
            if savings_target_NOK is not None
            else None
        )
        adj: dict[str, Decimal] = {}
        if adjustments:
            for name, delta in adjustments.items():
                adj[name] = _to_decimal(delta, field=f"adjustments[{name}]")

        # Build baseline window ending at the target month (oldest first).
        if months_back <= 0:
            months_back = 1
        if months_back > 24:
            months_back = 24
        months: list[date] = []
        y, mo = target.year, target.month
        # Start from the month BEFORE target (we don't include the target month
        # in its own baseline since it's the one we're planning).
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1
        for _ in range(months_back):
            months.append(date(y, mo, 1))
            mo -= 1
            if mo == 0:
                mo = 12
                y -= 1
        months.reverse()

        with session_scope() as s:
            txs = list_transactions(s, household_id)
            current = _current_budget_for_month(s, household_id, target)
            cats = list_categories(s, household_id)

        baseline = compute_planning_baseline(txs, months)
        proposal = suggest_allocations(
            baseline,
            target_month=target,
            current_budget=current,
            strategy=strategy,
            adjustments=adj or None,
            savings_target=savings,
        )

        # Surface category-name issues so the agent can ask the user to
        # rename/create before applying.
        known_names = {c.name for c in cats if not c.is_income}
        unknown = sorted(set(proposal.allocations) - known_names)
        notes = list(proposal.notes)
        if unknown:
            notes.append(
                "categories not in this household (won't apply until added): "
                + ", ".join(unknown)
            )

        diff_rows = diff_budget(current, proposal.allocations)
        token = _plan_token(target, proposal.allocations, savings)
        return {
            "ok": True,
            "month": target.isoformat(),
            "strategy": proposal.strategy,
            "plan_token": token,
            "allocations_NOK": {
                name: _decimal_to_str(amount)
                for name, amount in proposal.allocations.items()
            },
            "diff": [
                {
                    "category": r.category_name,
                    "current_NOK": _decimal_to_str(r.current),
                    "proposed_NOK": _decimal_to_str(r.proposed),
                    "delta_NOK": _decimal_to_str(r.delta),
                }
                for r in diff_rows
            ],
            "expected_income_NOK": _decimal_to_str(proposal.expected_income),
            "expected_expense_NOK": _decimal_to_str(proposal.expected_expense),
            "expected_net_NOK": _decimal_to_str(proposal.expected_net),
            "savings_target_NOK": (
                _decimal_to_str(proposal.savings_target)
                if proposal.savings_target is not None
                else None
            ),
            "feasibility": proposal.feasibility,
            "gap_NOK": _decimal_to_str(proposal.gap),
            "unknown_categories": unknown,
            "notes": notes,
        }

    def apply_budget_plan(
        month: str,
        allocations_NOK: dict[str, Any],
        plan_token: str,
        savings_target_NOK: str | None = None,
    ) -> dict:
        """Apply a previously drafted plan to the household's budgets. ALL or
        NOTHING: if any category fails (unknown name, invalid amount), no
        budgets are written and `ok=False` is returned with details. The agent
        must always relay this outcome to the user — including failures.

        Only call after the user has explicitly confirmed the proposal. The
        `plan_token` must match the one returned by `draft_budget_plan` for the
        exact same `month`, `allocations_NOK`, and `savings_target_NOK`; if the
        user changed any number, redraft first to get a new token.

        Args:
            month: Target month in YYYY-MM format.
            allocations_NOK: {category_name: amount as decimal string} from the
                draft, exactly as the user confirmed.
            plan_token: The token returned by `draft_budget_plan`.
            savings_target_NOK: Same value passed to draft (or None if absent).
        """
        target = _parse_month(month)

        try:
            allocations: dict[str, Decimal] = {
                name: _to_decimal(amount, field=f"allocations_NOK[{name}]")
                for name, amount in allocations_NOK.items()
            }
            savings = (
                _to_decimal(savings_target_NOK, field="savings_target_NOK")
                if savings_target_NOK is not None
                else None
            )
        except ValueError as exc:
            return {"ok": False, "applied_count": 0, "error": str(exc)}

        for name, amount in allocations.items():
            if amount < 0:
                return {
                    "ok": False,
                    "applied_count": 0,
                    "error": f"allocation for {name!r} is negative ({amount}); cannot apply",
                }

        expected = _plan_token(target, allocations, savings)
        if expected != plan_token:
            return {
                "ok": False,
                "applied_count": 0,
                "error": (
                    "plan_token mismatch — the allocations don't match the most recent "
                    "draft. Re-run draft_budget_plan and confirm the new proposal with "
                    "the user before applying."
                ),
            }

        # Single transaction: any error inside session_scope rolls everything
        # back, which is what "all or nothing" requires.
        try:
            with session_scope() as s:
                cats = list_categories(s, household_id)
                name_to_id = {
                    c.name: uuid.UUID(c.id) for c in cats if not c.is_income
                }
                missing = sorted(set(allocations) - set(name_to_id))
                if missing:
                    raise ValueError(
                        "categories not found in this household: "
                        + ", ".join(missing)
                    )
                applied = 0
                for name, amount in allocations.items():
                    upsert_budget(
                        s,
                        household_id,
                        month=target,
                        category_id=name_to_id[name],
                        amount=amount,
                    )
                    applied += 1
            return {
                "ok": True,
                "month": target.isoformat(),
                "applied_count": applied,
                "plan_token": plan_token,
            }
        except Exception as exc:  # noqa: BLE001 — surface any DB error to the user
            return {
                "ok": False,
                "applied_count": 0,
                "error": str(exc) or exc.__class__.__name__,
            }

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
        get_planning_baseline,
        draft_budget_plan,
        apply_budget_plan,
    ]
