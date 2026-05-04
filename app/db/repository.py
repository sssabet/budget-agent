"""DB → plain dataclass conversion.

The agent's tools never see SQLAlchemy ORM objects. They get the lightweight
dataclasses from app/tools/types.py. That makes the math layer testable in
isolation and prevents the agent from accidentally lazy-loading relationships.
"""
from __future__ import annotations

import uuid
from datetime import date, time
from decimal import Decimal

from sqlalchemy import func, select
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


def get_user_by_email(s: Session, email: str) -> m.User | None:
    return s.scalar(select(m.User).where(m.User.email == email.lower()))


def get_user_by_id(s: Session, user_id: uuid.UUID) -> m.User | None:
    return s.get(m.User, user_id)


def list_user_households(s: Session, user_id: uuid.UUID) -> list[m.Household]:
    return list(
        s.scalars(
            select(m.Household)
            .join(m.HouseholdUser, m.HouseholdUser.household_id == m.Household.id)
            .where(m.HouseholdUser.user_id == user_id)
            .order_by(m.Household.name)
        ).all()
    )


def get_or_create_user(
    s: Session, *, email: str, display_name: str
) -> tuple[m.User, bool]:
    """Look up a user by email or create one. Returns (user, created)."""
    email_norm = email.strip().lower()
    user = get_user_by_email(s, email_norm)
    if user is not None:
        return user, False
    user = m.User(email=email_norm, display_name=display_name or email_norm.split("@")[0])
    s.add(user)
    s.flush()
    return user, True


def ensure_personal_household(
    s: Session,
    user: m.User,
    *,
    default_categories: list[tuple[str, bool]] | None = None,
) -> m.Household:
    """Make sure the user has at least one household. If they already have one,
    return the first (alphabetical). Otherwise create a personal household named
    after them and seed the default expense/income categories.

    Why default categories? Without them, the dashboard is blank and the agent
    can't really answer "where did our money go?" — a brand-new user gets a
    sensible starting palette they can rename later.
    """
    existing = list_user_households(s, user.id)
    if existing:
        return existing[0]

    household = m.Household(name=f"{user.display_name}'s budget")
    s.add(household)
    s.flush()
    s.add(m.HouseholdUser(household_id=household.id, user_id=user.id, role="owner"))

    if default_categories:
        for name, is_income in default_categories:
            s.add(
                m.Category(
                    household_id=household.id, name=name, is_income=is_income
                )
            )
    s.flush()
    return household


def list_categories(s: Session, household_id: uuid.UUID) -> list[dto.Category]:
    rows = s.scalars(
        select(m.Category)
        .where(m.Category.household_id == household_id)
        .order_by(m.Category.name)
    ).all()
    return [_category_to_dto(c) for c in rows]


def list_household_members(s: Session, household_id: uuid.UUID) -> list[m.User]:
    return list(
        s.scalars(
            select(m.User)
            .join(m.HouseholdUser, m.HouseholdUser.user_id == m.User.id)
            .where(m.HouseholdUser.household_id == household_id)
            .order_by(m.User.display_name)
        ).all()
    )


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


def _next_month(month: date) -> date:
    first = month.replace(day=1)
    if first.month == 12:
        return first.replace(year=first.year + 1, month=1)
    return first.replace(month=first.month + 1)


def list_transaction_rows(
    s: Session,
    household_id: uuid.UUID,
    *,
    month: date | None = None,
    category_id: uuid.UUID | None = None,
    only_uncategorized: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[m.Transaction]:
    q = (
        select(m.Transaction)
        .where(m.Transaction.household_id == household_id)
        .options(
            selectinload(m.Transaction.category),
            selectinload(m.Transaction.paid_by),
            selectinload(m.Transaction.belongs_to),
        )
        .order_by(m.Transaction.date.desc().nullslast(), m.Transaction.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if month is not None:
        first = month.replace(day=1)
        q = q.where(m.Transaction.date >= first, m.Transaction.date < _next_month(first))
    if only_uncategorized:
        q = q.where(m.Transaction.category_id.is_(None))
    elif category_id is not None:
        q = q.where(m.Transaction.category_id == category_id)
    return list(s.scalars(q).all())


def create_transaction(
    s: Session,
    household_id: uuid.UUID,
    *,
    product: str,
    amount: Decimal,
    transaction_date: date,
    category_id: uuid.UUID | None,
    paid_by_user_id: uuid.UUID | None,
    belongs_to_user_id: uuid.UUID | None,
    description: str | None,
    date_is_estimated: bool = False,
) -> m.Transaction:
    if category_id is not None:
        category = s.get(m.Category, category_id)
        if category is None or category.household_id != household_id:
            raise ValueError("category does not belong to this household")

    for user_id in (paid_by_user_id, belongs_to_user_id):
        if user_id is None:
            continue
        member = s.scalar(
            select(m.HouseholdUser).where(
                m.HouseholdUser.household_id == household_id,
                m.HouseholdUser.user_id == user_id,
            )
        )
        if member is None:
            raise ValueError("user does not belong to this household")

    row = m.Transaction(
        household_id=household_id,
        date=transaction_date,
        date_is_estimated=date_is_estimated,
        product=product.strip(),
        amount=amount,
        paid_by_user_id=paid_by_user_id,
        belongs_to_user_id=belongs_to_user_id,
        category_id=category_id,
        description=description.strip() if description else None,
    )
    s.add(row)
    s.flush()
    s.refresh(row)
    return row


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


def create_category(
    s: Session,
    household_id: uuid.UUID,
    *,
    name: str,
    is_income: bool,
) -> m.Category:
    """Create a new category for the household. Case-insensitive name uniqueness."""
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("name is required")
    existing = s.scalar(
        select(m.Category).where(
            m.Category.household_id == household_id,
            func.lower(m.Category.name) == cleaned.lower(),
        )
    )
    if existing is not None:
        raise ValueError(f"category {cleaned!r} already exists")
    cat = m.Category(household_id=household_id, name=cleaned, is_income=is_income)
    s.add(cat)
    s.flush()
    return cat


def update_category(
    s: Session,
    household_id: uuid.UUID,
    category_id: uuid.UUID,
    *,
    name: str | None = None,
    is_income: bool | None = None,
) -> m.Category:
    cat = s.scalar(
        select(m.Category).where(
            m.Category.id == category_id,
            m.Category.household_id == household_id,
        )
    )
    if cat is None:
        raise ValueError("category not found")
    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("name is required")
        if len(cleaned) > 80:
            raise ValueError("name too long")
        dup = s.scalar(
            select(m.Category).where(
                m.Category.household_id == household_id,
                func.lower(m.Category.name) == cleaned.lower(),
                m.Category.id != category_id,
            )
        )
        if dup is not None:
            raise ValueError(f"category {cleaned!r} already exists")
        cat.name = cleaned
    if is_income is not None:
        cat.is_income = is_income
    s.flush()
    return cat


def delete_category(
    s: Session, household_id: uuid.UUID, category_id: uuid.UUID
) -> int:
    """Delete a category. Transactions referencing it become uncategorized
    (FK is ON DELETE SET NULL). Budgets for the category are cascade-deleted.
    Returns the number of transactions whose category became NULL.
    """
    cat = s.scalar(
        select(m.Category).where(
            m.Category.id == category_id,
            m.Category.household_id == household_id,
        )
    )
    if cat is None:
        raise ValueError("category not found")
    affected = s.scalar(
        select(func.count())
        .select_from(m.Transaction)
        .where(m.Transaction.category_id == category_id)
    )
    s.delete(cat)
    s.flush()
    return int(affected or 0)


def update_transaction(
    s: Session,
    household_id: uuid.UUID,
    transaction_id: uuid.UUID,
    fields: dict,
) -> m.Transaction:
    """Apply a PATCH-style update. Only keys present in `fields` are touched
    — passing {"category_id": None} clears the category; omitting the key
    leaves it alone. Pydantic's exclude_unset gives the API layer the right
    dict shape to forward here.
    """
    tx = s.scalar(
        select(m.Transaction)
        .where(
            m.Transaction.id == transaction_id,
            m.Transaction.household_id == household_id,
        )
        .options(
            selectinload(m.Transaction.category),
            selectinload(m.Transaction.paid_by),
            selectinload(m.Transaction.belongs_to),
        )
    )
    if tx is None:
        raise ValueError("transaction not found")
    if "product" in fields:
        cleaned = (fields["product"] or "").strip()
        if not cleaned:
            raise ValueError("product is required")
        tx.product = cleaned
    if "amount" in fields:
        amount = fields["amount"]
        if amount is None or amount <= 0:
            raise ValueError("amount must be positive")
        tx.amount = amount
    if "date" in fields:
        tx.date = fields["date"]  # may be None
    if "date_is_estimated" in fields:
        tx.date_is_estimated = bool(fields["date_is_estimated"])
    if "category_id" in fields:
        tx.category_id = fields["category_id"]
        tx.needs_review = tx.category_id is None
    if "paid_by_user_id" in fields:
        tx.paid_by_user_id = fields["paid_by_user_id"]
    if "belongs_to_user_id" in fields:
        tx.belongs_to_user_id = fields["belongs_to_user_id"]
    if "description" in fields:
        desc = fields["description"]
        tx.description = (desc.strip() if isinstance(desc, str) and desc.strip() else None)
    s.flush()
    s.refresh(tx)
    return tx


def delete_transaction(
    s: Session, household_id: uuid.UUID, transaction_id: uuid.UUID
) -> bool:
    rows = (
        s.query(m.Transaction)
        .filter(
            m.Transaction.id == transaction_id,
            m.Transaction.household_id == household_id,
        )
        .delete(synchronize_session=False)
    )
    return rows > 0


def upsert_budget(
    s: Session,
    household_id: uuid.UUID,
    *,
    month: date,
    category_id: uuid.UUID,
    amount: Decimal,
) -> m.Budget:
    """Set or update the monthly budget for a category. Idempotent."""
    if amount < 0:
        raise ValueError("amount must be non-negative")
    first = month.replace(day=1)
    existing = s.scalar(
        select(m.Budget).where(
            m.Budget.household_id == household_id,
            m.Budget.month == first,
            m.Budget.category_id == category_id,
        )
    )
    if existing is None:
        existing = m.Budget(
            household_id=household_id,
            month=first,
            category_id=category_id,
            amount=amount,
        )
        s.add(existing)
        s.flush()
    else:
        existing.amount = amount
        s.flush()
    return existing


def delete_all_transactions(s: Session, household_id: uuid.UUID) -> int:
    """Wipe transactions for a household. Returns deleted count."""
    return (
        s.query(m.Transaction)
        .filter(m.Transaction.household_id == household_id)
        .delete(synchronize_session=False)
    )


def update_household_name(
    s: Session, household_id: uuid.UUID, *, name: str
) -> m.Household:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("name is required")
    if len(cleaned) > 120:
        raise ValueError("name too long")
    household = s.get(m.Household, household_id)
    if household is None:
        raise ValueError("household not found")
    household.name = cleaned
    s.flush()
    return household


def add_household_member_by_email(
    s: Session,
    household_id: uuid.UUID,
    *,
    email: str,
    display_name: str | None = None,
    role: str = "member",
) -> tuple[m.User, bool]:
    """Add a user to the household by email.

    The user row is created if it doesn't exist yet — that way when they sign
    in for the first time, `get_or_create_user` finds them already and
    `ensure_personal_household` sees they have a household, so we *don't*
    accidentally create a duplicate "personal" household for them. Returns
    (user, membership_created). Idempotent: re-adding an existing member
    returns membership_created=False.
    """
    cleaned = email.strip().lower()
    if not cleaned or "@" not in cleaned:
        raise ValueError("email looks invalid")

    user = get_user_by_email(s, cleaned)
    if user is None:
        local = display_name or cleaned.split("@", 1)[0]
        user = m.User(email=cleaned, display_name=(local or cleaned)[:120])
        s.add(user)
        s.flush()

    membership = s.get(m.HouseholdUser, (household_id, user.id))
    created = False
    if membership is None:
        s.add(
            m.HouseholdUser(
                household_id=household_id, user_id=user.id, role=role
            )
        )
        s.flush()
        created = True
    return user, created


def upsert_notification_subscription(
    s: Session,
    *,
    user_id: uuid.UUID,
    household_id: uuid.UUID,
    endpoint: str,
    p256dh: str,
    auth: str,
    timezone: str,
    reminder_time: time,
    enabled: bool,
) -> m.NotificationSubscription:
    row = s.scalar(
        select(m.NotificationSubscription).where(m.NotificationSubscription.endpoint == endpoint)
    )
    if row is None:
        row = m.NotificationSubscription(
            user_id=user_id,
            household_id=household_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            timezone=timezone,
            reminder_time=reminder_time,
            enabled=enabled,
        )
        s.add(row)
    else:
        row.user_id = user_id
        row.household_id = household_id
        row.p256dh = p256dh
        row.auth = auth
        row.timezone = timezone
        row.reminder_time = reminder_time
        row.enabled = enabled
    s.flush()
    return row


def list_enabled_notification_subscriptions(s: Session) -> list[m.NotificationSubscription]:
    return list(
        s.scalars(
            select(m.NotificationSubscription).where(
                m.NotificationSubscription.enabled.is_(True)
            )
        ).all()
    )


def mark_subscription_reminded(
    s: Session, subscription_id: uuid.UUID, reminded_on: date
) -> None:
    row = s.get(m.NotificationSubscription, subscription_id)
    if row is not None:
        row.last_reminded_on = reminded_on
