"""Bootstrap the schema and (optionally) seed sample data.

Prototype-grade: uses Base.metadata.create_all rather than Alembic. Good enough
while the schema churns. We'll switch to Alembic before any cloud deployment.

Usage:
  python -m app.db.init_db --reset --seed
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.config import settings
from app.db.models import (
    Base,
    Category,
    Household,
    HouseholdUser,
    User,
)
from app.db.session import engine, session_scope

DEFAULT_CATEGORIES = [
    # Income
    ("Salary", True),
    ("Other Income", True),
    # Housing
    ("Rent / Mortgage", False),
    ("Utilities", False),
    ("Home & Appliances", False),
    # Food
    ("Groceries", False),
    ("Eating Out", False),
    # Mobility
    ("Transport", False),
    ("Car", False),
    # Lifestyle
    ("Health & Wellness", False),
    ("Subscriptions & Entertainment", False),
    ("Personal Care", False),
    ("Clothing", False),
    ("Education", False),
    ("Travel", False),
    ("Gifts", False),
    # Money flow
    ("Investment", False),
    ("Miscellaneous", False),
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

        for name, is_income in DEFAULT_CATEGORIES:
            s.add(Category(household_id=household.id, name=name, is_income=is_income))
        s.flush()

        print(f"Seeded household '{household.name}' with {len(DEFAULT_CATEGORIES)} default categories.")


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
