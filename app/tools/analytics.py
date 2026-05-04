"""Trend / pattern analytics over the transaction list.

These functions answer the questions a budget coach gets every Sunday:
  - Are we trending up or down month-over-month?
  - Which merchants quietly eat the budget?
  - What looks like a recurring subscription we forgot we had?

All pure functions on the existing Transaction DTO list. They don't know about
BigQuery, Postgres, or the agent — that's deliberate. The same code answers
the agent's tool calls today and feeds the BQ sync (`scripts/sync_to_bigquery.py`)
for external dashboards.

Architectural rule: analytics, like all math in this project, is deterministic
Python. The LLM phrases the answer; it never derives the numbers.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Iterable

from app.tools.budget_math import (
    EXCLUDED_COST_CATEGORIES,
    spend_by_category,
    total_expense,
    total_income,
)
from app.tools.types import Transaction

ZERO = Decimal("0")


@dataclass(frozen=True)
class MonthSpend:
    month: date  # first of the month
    total_income: Decimal
    total_expense: Decimal
    net: Decimal
    by_category: dict[str, Decimal]


@dataclass(frozen=True)
class MerchantTotal:
    merchant: str  # the displayed product name (cased like the most recent occurrence)
    occurrences: int
    total: Decimal


@dataclass(frozen=True)
class RecurringSubscription:
    merchant: str
    months_seen: int
    last_seen: date
    typical_monthly_amount: Decimal  # median across observed months
    category: str | None  # most common category among matching transactions, if any


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _is_excluded_cost(t: Transaction) -> bool:
    return (
        t.category is not None
        and t.category.name.strip().lower() in EXCLUDED_COST_CATEGORIES
    )


def _normalize_merchant(product: str) -> str:
    """Cheap merchant key: lowercase + collapse whitespace. Distinguishes
    "REMA 1000" from "REMA 1000 Storo" only if the trailing site is in the
    name — usually fine for a household. We don't try to merge "REMA" with
    "REMA 1000" because that loses a real signal (which store)."""
    return " ".join(product.lower().split())


def month_over_month_spend(
    transactions: Iterable[Transaction], months: Iterable[date]
) -> list[MonthSpend]:
    """Per-month income/expense/net for an explicit list of months.

    The caller decides which months are interesting; we don't infer "the last
    six" because the right window depends on what the user asked. Pass the
    month explicitly (first-of-month dates) and we'll preserve order.
    """
    txs = list(transactions)
    out: list[MonthSpend] = []
    for m in months:
        first = _first_of_month(m)
        income = total_income(txs, first)
        expense = total_expense(txs, first)
        out.append(
            MonthSpend(
                month=first,
                total_income=income,
                total_expense=expense,
                net=income - expense,
                by_category=spend_by_category(txs, first),
            )
        )
    return out


def top_merchants(
    transactions: Iterable[Transaction],
    month: date | None = None,
    n: int = 10,
) -> list[MerchantTotal]:
    """Top N merchants by total expense for a month (or all-time if month=None).

    Income transactions and excluded-cost categories (Investment) are dropped:
    "top merchants" should answer "where is the money actually going."
    """
    if n <= 0:
        return []

    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    display_name: dict[str, str] = {}

    for t in transactions:
        if t.category is not None and t.category.is_income:
            continue
        if _is_excluded_cost(t):
            continue
        if month is not None:
            if t.date is None:
                continue
            if _first_of_month(t.date) != _first_of_month(month):
                continue
        key = _normalize_merchant(t.product)
        if not key:
            continue
        counts[key] += 1
        totals[key] += t.amount
        display_name[key] = t.product  # last-write-wins; fine for display

    rows = [
        MerchantTotal(
            merchant=display_name[k], occurrences=counts[k], total=totals[k]
        )
        for k in totals
    ]
    rows.sort(key=lambda r: (-r.total, r.merchant.lower()))
    return rows[:n]


def find_recurring_subscriptions(
    transactions: Iterable[Transaction],
    *,
    min_months: int = 3,
    amount_tolerance: Decimal = Decimal("0.20"),
) -> list[RecurringSubscription]:
    """Detect merchants that look like subscriptions.

    Definition: same merchant appears in at least `min_months` distinct months
    AND every observed amount is within `amount_tolerance` (default 20%) of
    the median amount. The amount-stability check is what distinguishes a
    Netflix charge from "I happened to buy groceries every month."

    Income and Investment categories are excluded.
    """
    by_merchant: dict[str, list[Transaction]] = defaultdict(list)
    display_name: dict[str, str] = {}

    for t in transactions:
        if t.date is None:
            continue
        if t.category is not None and t.category.is_income:
            continue
        if _is_excluded_cost(t):
            continue
        key = _normalize_merchant(t.product)
        if not key:
            continue
        by_merchant[key].append(t)
        display_name[key] = t.product

    out: list[RecurringSubscription] = []
    for key, txs in by_merchant.items():
        months_set = {_first_of_month(t.date) for t in txs if t.date is not None}
        if len(months_set) < min_months:
            continue

        amounts = [t.amount for t in txs]
        med = Decimal(str(median(amounts)))
        if med == ZERO:
            continue
        if not all(
            abs(a - med) / med <= amount_tolerance for a in amounts
        ):
            continue

        last_seen = max(t.date for t in txs if t.date is not None)
        # Pick the most common category name among the matching transactions.
        cat_counts: dict[str, int] = defaultdict(int)
        for t in txs:
            if t.category is not None:
                cat_counts[t.category.name] += 1
        category = (
            max(cat_counts.items(), key=lambda kv: kv[1])[0]
            if cat_counts
            else None
        )

        out.append(
            RecurringSubscription(
                merchant=display_name[key],
                months_seen=len(months_set),
                last_seen=last_seen,
                typical_monthly_amount=med,
                category=category,
            )
        )

    out.sort(key=lambda r: (-r.typical_monthly_amount, r.merchant.lower()))
    return out
