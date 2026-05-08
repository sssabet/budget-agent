"""Deterministic budget calculations.

Architectural rule: the LLM never does math. It calls these functions, gets back
structured numbers, and explains them in natural language. This is what keeps the
agent auditable and stops it from inventing "you spent 4,237 NOK on coffee" when
the real number is 1,820.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.tools.types import (
    AllocationProposal,
    Budget,
    BudgetDiffRow,
    CategoryBaseline,
    CategoryBudgetReport,
    MonthSummary,
    PlanningBaseline,
    Transaction,
)

ZERO = Decimal("0")
NEAR_BUDGET_THRESHOLD = Decimal("0.85")  # 85% of budget = "near"
EXCLUDED_COST_CATEGORIES = {"investment"}


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _in_month(t: Transaction, month: date) -> bool:
    if t.date is None:
        return False
    return _first_of_month(t.date) == _first_of_month(month)


def _is_excluded_cost_category(t: Transaction) -> bool:
    return (
        t.category is not None
        and t.category.name.strip().lower() in EXCLUDED_COST_CATEGORIES
    )


def spend_by_category(
    transactions: list[Transaction], month: date
) -> dict[str, Decimal]:
    """Sum of expense-category transactions in a month, grouped by category name.

    Income categories are excluded. Uncategorized transactions are bucketed under
    "(uncategorized)" so they're visible rather than silently dropped.
    """
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in transactions:
        if not _in_month(t, month):
            continue
        if t.category is not None and t.category.is_income:
            continue
        name = t.category.name if t.category else "(uncategorized)"
        totals[name] += t.amount
    return dict(totals)


def total_income(transactions: list[Transaction], month: date) -> Decimal:
    return sum(
        (t.amount for t in transactions
         if _in_month(t, month) and t.category and t.category.is_income),
        start=ZERO,
    )


def total_expense(transactions: list[Transaction], month: date) -> Decimal:
    return sum(
        (t.amount for t in transactions
         if _in_month(t, month)
         and not (t.category and t.category.is_income)
         and not _is_excluded_cost_category(t)),
        start=ZERO,
    )


def compare_budget_vs_actual(
    transactions: list[Transaction],
    budgets: list[Budget],
    categories_by_id: dict[str, str],  # id -> name
    month: date,
) -> list[CategoryBudgetReport]:
    """Per-category report for the given month.

    Includes every category that has either a budget or any actual spend in the month.
    Status:
      - "over" if actual > budgeted
      - "near" if actual >= 85% of budgeted (and budgeted > 0)
      - "under" otherwise
    """
    actuals = spend_by_category(transactions, month)

    month_first = _first_of_month(month)
    budgeted_by_name: dict[str, Decimal] = {}
    for b in budgets:
        if _first_of_month(b.month) != month_first:
            continue
        name = categories_by_id.get(b.category_id)
        if name is None:
            continue
        budgeted_by_name[name] = b.amount

    all_names = sorted(set(actuals) | set(budgeted_by_name))
    reports: list[CategoryBudgetReport] = []
    for name in all_names:
        budgeted = budgeted_by_name.get(name, ZERO)
        actual = actuals.get(name, ZERO)
        variance = budgeted - actual
        status = _status(budgeted, actual)
        reports.append(
            CategoryBudgetReport(
                category_name=name,
                budgeted=budgeted,
                actual=actual,
                variance=variance,
                status=status,
            )
        )
    return reports


def _status(budgeted: Decimal, actual: Decimal) -> str:
    if budgeted == ZERO:
        return "over" if actual > ZERO else "under"
    if actual > budgeted:
        return "over"
    if actual >= budgeted * NEAR_BUDGET_THRESHOLD:
        return "near"
    return "under"


def list_uncategorized(
    transactions: list[Transaction], month: date | None = None
) -> list[Transaction]:
    out = [t for t in transactions if t.category is None]
    if month is not None:
        out = [t for t in out if _in_month(t, month)]
    return out


def spend_by_owner(
    transactions: list[Transaction], month: date
) -> dict[str, Decimal]:
    """Grouped by belongs_to (None -> "Household"). Expense rows only."""
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in transactions:
        if not _in_month(t, month):
            continue
        if t.category and t.category.is_income:
            continue
        if _is_excluded_cost_category(t):
            continue
        owner = t.belongs_to or "Household"
        totals[owner] += t.amount
    return dict(totals)


def summarize_month(
    transactions: list[Transaction],
    budgets: list[Budget],
    categories_by_id: dict[str, str],
    month: date,
) -> MonthSummary:
    income = total_income(transactions, month)
    expense = total_expense(transactions, month)
    by_cat = spend_by_category(transactions, month)
    reports = compare_budget_vs_actual(transactions, budgets, categories_by_id, month)
    over = [r for r in reports if r.status == "over"]
    over.sort(key=lambda r: r.variance)  # most-over first (most negative variance)

    in_month = [t for t in transactions if _in_month(t, month)]
    uncategorized = sum(1 for t in in_month if t.category is None)
    estimated = sum(1 for t in in_month if t.date_is_estimated)

    return MonthSummary(
        month=_first_of_month(month),
        total_income=income,
        total_expense=expense,
        net=income - expense,
        by_category=by_cat,
        over_budget_categories=over,
        uncategorized_count=uncategorized,
        estimated_date_count=estimated,
    )


# ---------------------------------------------------------------------------
# Planning helpers
#
# These power the "draft a budget for next month" flow. The shape is:
#   compute_planning_baseline(...)  -> stats the LLM can talk about
#   suggest_allocations(...)        -> deterministic per-category numbers
#   diff_budget(...)                -> old/new/delta for showing the user
#
# The LLM never picks the numbers; it picks the *strategy* and any user-driven
# adjustments. That keeps proposals reproducible and auditable.
# ---------------------------------------------------------------------------

ROUND_STEP = Decimal("100")  # round allocations to nearest 100 NOK for readability
TIGHT_MARGIN_PCT = Decimal("0.05")  # within 5% of income+savings is "tight"


def _round_to_step(amount: Decimal, step: Decimal = ROUND_STEP) -> Decimal:
    if step <= ZERO:
        return amount
    # Round half-up to the nearest step; never produce negatives.
    if amount <= ZERO:
        return ZERO
    quantized = (amount / step).quantize(Decimal("1"), rounding="ROUND_HALF_UP") * step
    return quantized


def _percentile(sorted_values: list[Decimal], pct: Decimal) -> Decimal:
    """Linear-interpolation percentile on a pre-sorted list. pct in [0,1]."""
    if not sorted_values:
        return ZERO
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * Decimal(len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - Decimal(lo)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def compute_planning_baseline(
    transactions: list[Transaction], months: list[date]
) -> PlanningBaseline:
    """Per-category statistics across `months` (oldest first), zero-filling
    months where the category had no spend so averages reflect "this is what
    a typical month looks like" rather than "this is what an active month
    looks like".

    Income is reported as a single avg_monthly_income figure; we don't break
    down income by source (the agent's planning today is expense-side).
    """
    if not months:
        return PlanningBaseline(
            months=[],
            avg_monthly_income=ZERO,
            avg_monthly_expense=ZERO,
            avg_monthly_net=ZERO,
            categories=[],
        )

    months_sorted = sorted({_first_of_month(m) for m in months})
    n = Decimal(len(months_sorted))

    income_total = ZERO
    expense_total = ZERO
    # category_name -> list[Decimal], one slot per month in order.
    per_cat: dict[str, list[Decimal]] = {}
    is_income_for: dict[str, bool] = {}

    for month_first in months_sorted:
        income_total += total_income(transactions, month_first)
        expense_total += total_expense(transactions, month_first)

        # Spend by expense category (already excludes income + investment).
        spend = spend_by_category(transactions, month_first)
        seen_this_month = set(spend.keys())
        for name, amount in spend.items():
            per_cat.setdefault(name, [ZERO] * len(months_sorted))
            per_cat[name][months_sorted.index(month_first)] = amount
            is_income_for[name] = False

        # Income categories — track separately so the user can see income
        # category trends if they ever ask. We zero-fill the same way.
        for t in transactions:
            if not _in_month(t, month_first):
                continue
            if t.category and t.category.is_income:
                name = t.category.name
                per_cat.setdefault(name, [ZERO] * len(months_sorted))
                idx = months_sorted.index(month_first)
                per_cat[name][idx] += t.amount
                is_income_for[name] = True
                seen_this_month.add(name)

    categories: list[CategoryBaseline] = []
    for name, series in per_cat.items():
        observed = [v for v in series if v > ZERO]
        observed_sorted = sorted(observed)
        avg = sum(series, start=ZERO) / n if n > 0 else ZERO
        med = _percentile(observed_sorted, Decimal("0.5")) if observed_sorted else ZERO
        p90 = _percentile(observed_sorted, Decimal("0.9")) if observed_sorted else ZERO
        last = series[-1]
        floor = (
            min(observed) if len(observed) >= 3 else ZERO
        )  # treat as a recurring floor only with enough signal
        categories.append(
            CategoryBaseline(
                category_name=name,
                is_income=is_income_for.get(name, False),
                months_observed=len(observed),
                avg_monthly=avg,
                median_monthly=med,
                p90_monthly=p90,
                last_month=last,
                recurring_floor=floor,
            )
        )

    categories.sort(key=lambda c: (c.is_income, -c.avg_monthly))

    avg_income = income_total / n if n > 0 else ZERO
    avg_expense = expense_total / n if n > 0 else ZERO
    return PlanningBaseline(
        months=months_sorted,
        avg_monthly_income=avg_income,
        avg_monthly_expense=avg_expense,
        avg_monthly_net=avg_income - avg_expense,
        categories=categories,
    )


def _expense_baselines(baseline: PlanningBaseline) -> list[CategoryBaseline]:
    return [c for c in baseline.categories if not c.is_income]


def suggest_allocations(
    baseline: PlanningBaseline,
    *,
    target_month: date,
    current_budget: dict[str, Decimal],
    strategy: str,
    adjustments: dict[str, Decimal] | None = None,
    savings_target: Decimal | None = None,
) -> AllocationProposal:
    """Produce a per-category proposal for `target_month`. Pure function.

    Strategies:
      - "keep": copy current_budget. If empty, fall back to last-month spend
        per category, rounded.
      - "rolling_average": use avg_monthly per expense category, rounded.
      - "adjust": start from current_budget (or fall back to last_month) and
        apply per-category deltas in `adjustments`. Negative deltas clamp to 0.

    Allocations are rounded to ROUND_STEP for readability. Income is not
    allocated; the proposal compares total expense against avg_monthly_income
    to flag feasibility.
    """
    notes: list[str] = []
    target = _first_of_month(target_month)
    expense_cats = _expense_baselines(baseline)
    last_by_name = {c.category_name: c.last_month for c in expense_cats}
    avg_by_name = {c.category_name: c.avg_monthly for c in expense_cats}

    if strategy == "keep":
        if current_budget:
            raw = dict(current_budget)
        else:
            raw = dict(last_by_name)
            if raw:
                notes.append(
                    "no current budget for the target month — used last month's actual spend as the starting point"
                )
            else:
                notes.append("no current budget and no prior spend — proposing zero across the board")
    elif strategy == "rolling_average":
        if not avg_by_name:
            notes.append("no historical spend in the window — nothing to average")
        raw = dict(avg_by_name)
    elif strategy == "adjust":
        if current_budget:
            raw = dict(current_budget)
        elif last_by_name:
            raw = dict(last_by_name)
            notes.append(
                "no current budget — used last month's spend as the starting point before applying adjustments"
            )
        else:
            raw = {}
            notes.append("no current budget and no prior spend to adjust from")
        if adjustments:
            unknown = [n for n in adjustments if n not in raw and n not in avg_by_name]
            if unknown:
                notes.append(
                    "adjustments referenced unknown categories: " + ", ".join(sorted(unknown))
                )
            for name, delta in adjustments.items():
                base = raw.get(name, last_by_name.get(name, avg_by_name.get(name, ZERO)))
                raw[name] = base + delta
    else:
        raise ValueError(f"unknown strategy: {strategy!r}")

    allocations = {name: max(ZERO, _round_to_step(amount)) for name, amount in raw.items()}
    # Drop categories that round to zero with no current budget — keeps the
    # proposal clean. We still keep zeros that explicitly came from current
    # budget so the user sees they were considered.
    allocations = {
        name: amount
        for name, amount in allocations.items()
        if amount > ZERO or name in current_budget
    }

    expected_expense = sum(allocations.values(), start=ZERO)
    expected_income = baseline.avg_monthly_income
    expected_net = expected_income - expected_expense

    if expected_income <= ZERO:
        feasibility = "unknown"
        gap = ZERO
        notes.append("no income data in the baseline window — feasibility unknown")
    elif savings_target is not None:
        gap = expected_income - expected_expense - savings_target
        if gap >= ZERO:
            feasibility = "fits"
        elif gap >= -(expected_income * TIGHT_MARGIN_PCT):
            feasibility = "tight"
        else:
            feasibility = "overshoots"
    else:
        gap = expected_net
        if gap >= ZERO:
            feasibility = "fits"
        elif gap >= -(expected_income * TIGHT_MARGIN_PCT):
            feasibility = "tight"
        else:
            feasibility = "overshoots"

    return AllocationProposal(
        month=target,
        strategy=strategy,
        allocations=dict(sorted(allocations.items())),
        expected_income=expected_income,
        expected_expense=expected_expense,
        expected_net=expected_net,
        savings_target=savings_target,
        feasibility=feasibility,
        gap=gap,
        notes=notes,
    )


def diff_budget(
    current_budget: dict[str, Decimal], proposed: dict[str, Decimal]
) -> list[BudgetDiffRow]:
    """Old/new/delta per category, sorted by largest absolute change first."""
    names = sorted(set(current_budget) | set(proposed))
    rows = [
        BudgetDiffRow(
            category_name=name,
            current=current_budget.get(name, ZERO),
            proposed=proposed.get(name, ZERO),
            delta=proposed.get(name, ZERO) - current_budget.get(name, ZERO),
        )
        for name in names
    ]
    rows.sort(key=lambda r: (-abs(r.delta), r.category_name))
    return rows
