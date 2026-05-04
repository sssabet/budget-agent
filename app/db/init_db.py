"""Bootstrap the schema and (optionally) seed sample data.

Prototype-grade: uses Base.metadata.create_all rather than Alembic. Good enough
while the schema churns. We'll switch to Alembic before any cloud deployment.

Usage:
  python -m app.db.init_db --reset --seed
"""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.config import settings
from app.db.models import (
    Base,
    Budget,
    Category,
    Household,
    HouseholdUser,
    User,
)
from app.db.session import engine, session_scope

DEFAULT_CATEGORIES = [
    ("Salary", True),
    ("Investment", False),
    ("Car", False),
    ("Eating_out", False),
    ("Health & Wellness", False),
    ("Grocery", False),
    ("Subscriptions / Entertaiment", False),
    ("Education", False),
    ("Utilities", False),
    ("Liam - Leisure", False),
    ("Transport", False),
    ("admin", False),
    ("Liam - Essential", False),
    ("Mortgage", False),
    ("Clothing", False),
    ("Home appliance", False),
    ("Gift", False),
    ("Travel", False),
]


def create_schema(reset: bool = False) -> None:
    eng = engine()
    if reset:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)


def seed() -> None:
    cfg = settings()
    with session_scope() as s:
        # idempotent: skip if our seed household already exists
        existing = s.scalar(select(Household).where(Household.name == cfg.default_household_name))
        if existing is not None:
            print(f"Household '{existing.name}' already seeded; skipping.")
            return

        household = Household(name=cfg.default_household_name)
        s.add(household)
        s.flush()

        saeed = User(email=cfg.seed_user_email, display_name="Saeed")
        maryam = User(email=cfg.seed_partner_email, display_name="Maryam")
        s.add_all([saeed, maryam])
        s.flush()

        s.add_all([
            HouseholdUser(household_id=household.id, user_id=saeed.id, role="owner"),
            HouseholdUser(household_id=household.id, user_id=maryam.id, role="member"),
        ])

        cats: dict[str, Category] = {}
        for name, is_income in DEFAULT_CATEGORIES:
            c = Category(household_id=household.id, name=name, is_income=is_income)
            s.add(c)
            cats[name] = c
        s.flush()

        may = date(2026, 5, 1)

        # May budget — zero-based envelopes
        budget_amounts = {
            "Utilities": "7700",
            "Car": "0",
            "Clothing": "800",
            "Eating_out": "3000",
            "Education": "3000",
            "Gift": "300",
            "Grocery": "8000",
            "Liam - Leisure": "800",
            "Transport": "1900",
            "Subscriptions / Entertaiment": "500",
            "Travel": "1000",
            "Mortgage": "35000",
        }
        for name, amount in budget_amounts.items():
            s.add(Budget(
                household_id=household.id,
                month=may,
                category_id=cats[name].id,
                amount=Decimal(amount),
            ))

        print(f"Seeded household '{household.name}' with 0 transactions and {len(budget_amounts)} budget lines.")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true", help="Drop all tables before creating")
    p.add_argument("--seed", action="store_true", help="Insert sample household + transactions")
    args = p.parse_args()
    create_schema(reset=args.reset)
    if args.seed:
        seed()


if __name__ == "__main__":
    main()
