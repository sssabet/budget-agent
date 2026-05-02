"""Plain dataclasses the budget-math tools operate on.

Why not use SQLAlchemy ORM rows directly? Two reasons:
1. Pure-function tools become trivially testable — no DB, no fixtures, just lists.
2. The agent layer can be swapped to read from any source (CSV, BigQuery, mock) without
   touching the math.

The repository layer (app/db/repository.py) converts ORM rows into these.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    is_income: bool


@dataclass(frozen=True)
class Transaction:
    id: str
    date: date | None
    date_is_estimated: bool
    product: str
    amount: Decimal  # always positive; sign comes from Category.is_income
    paid_by: str | None  # user display_name or None
    belongs_to: str | None  # user display_name or None (None = household)
    category: Category | None
    description: str | None
    needs_review: bool


@dataclass(frozen=True)
class Budget:
    month: date  # first of the month
    category_id: str
    amount: Decimal


@dataclass(frozen=True)
class CategoryBudgetReport:
    category_name: str
    budgeted: Decimal
    actual: Decimal
    variance: Decimal  # budgeted - actual; negative = over budget
    status: str  # "under" | "near" | "over"


@dataclass(frozen=True)
class MonthSummary:
    month: date
    total_income: Decimal
    total_expense: Decimal
    net: Decimal
    by_category: dict[str, Decimal]  # expense categories only
    over_budget_categories: list[CategoryBudgetReport]
    uncategorized_count: int
    estimated_date_count: int  # how many transactions had only month-precision dates
