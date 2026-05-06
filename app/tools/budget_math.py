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
    Budget,
    CategoryBudgetReport,
    MonthSummary,
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
