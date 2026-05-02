"""DB → plain dataclass conversion.

The agent's tools never see SQLAlchemy ORM objects. They get the lightweight
dataclasses from app/tools/types.py. That makes the math layer testable in
isolation and prevents the agent from accidentally lazy-loading relationships.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import models as m
from app.tools import types as dto


def _category_to_dto(c: m.Category) -> dto.Category:
    return dto.Category(id=str(c.id), name=c.name, is_income=c.is_income)


def _transaction_to_dto(t: m.Transaction) -> dto.Transaction:
    return dto.Transaction(
        id=str(t.id),
        date=t.date,
        date_is_estimated=t.date_is_estimated,
        product=t.product,
        amount=t.amount,
        paid_by=t.paid_by.display_name if t.paid_by else None,
        belongs_to=t.belongs_to.display_name if t.belongs_to else None,
        category=_category_to_dto(t.category) if t.category else None,
        description=t.description,
        needs_review=t.needs_review,
    )


def get_household_by_name(s: Session, name: str) -> m.Household | None:
    return s.scalar(select(m.Household).where(m.Household.name == name))


def get_household_by_id(s: Session, household_id: uuid.UUID) -> m.Household | None:
    return s.get(m.Household, household_id)


def list_categories(s: Session, household_id: uuid.UUID) -> list[dto.Category]:
    rows = s.scalars(
        select(m.Category)
        .where(m.Category.household_id == household_id)
        .order_by(m.Category.name)
    ).all()
    return [_category_to_dto(c) for c in rows]


def categories_by_id(s: Session, household_id: uuid.UUID) -> dict[str, str]:
    """{category_id (str): category_name} — used by compare_budget_vs_actual."""
    return {c.id: c.name for c in list_categories(s, household_id)}


def list_transactions(
    s: Session,
    household_id: uuid.UUID,
    *,
    month: date | None = None,
) -> list[dto.Transaction]:
    """Returns all transactions for a household, optionally filtered to a month
    (matched on the *first-of-month* of `t.date`, so estimated dates still match).
    """
    q = (
        select(m.Transaction)
        .where(m.Transaction.household_id == household_id)
        .options(
            selectinload(m.Transaction.category),
            selectinload(m.Transaction.paid_by),
            selectinload(m.Transaction.belongs_to),
        )
        .order_by(m.Transaction.date.desc().nullslast())
    )
    rows = s.scalars(q).all()
    txs = [_transaction_to_dto(t) for t in rows]
    if month is not None:
        first = month.replace(day=1)
        txs = [
            t for t in txs
            if t.date is not None and t.date.replace(day=1) == first
        ]
    return txs


def list_all_transactions_dto(
    s: Session, household_id: uuid.UUID
) -> list[dto.Transaction]:
    return list_transactions(s, household_id)


def list_budgets(
    s: Session, household_id: uuid.UUID, *, month: date | None = None
) -> list[dto.Budget]:
    q = select(m.Budget).where(m.Budget.household_id == household_id)
    if month is not None:
        q = q.where(m.Budget.month == month.replace(day=1))
    rows = s.scalars(q).all()
    return [
        dto.Budget(
            month=b.month,
            category_id=str(b.category_id),
            amount=b.amount,
        )
        for b in rows
    ]
