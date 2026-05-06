"""Tests for the CSV importer's auto-create-category behavior.

The interesting decisions:
- Unknown category text creates a new Category instead of being silently dropped.
- Case variants ('food' / 'FOOD' / 'Food') collapse into one category, title-cased.
- create_missing_categories=False preserves the old strict behavior.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, Category, Household, HouseholdUser, Transaction, User
from app.tools.csv_import import import_csv


@pytest.fixture
def session() -> Session:
    # Use SQLite in-memory for speed. The schema is portable; Postgres-specific
    # types (UUID) are fine because SQLAlchemy maps them.
    eng = create_engine("sqlite:///:memory:")
    # SQLAlchemy's UUID column type works on SQLite via CHAR(32). Force.
    from sqlalchemy.dialects import sqlite
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


@pytest.fixture
def household(session: Session) -> Household:
    h = Household(name="Test")
    saeed = User(email="s@x", display_name="Saeed")
    partner = User(email="p@x", display_name="Partner")
    session.add_all([h, saeed, partner])
    session.flush()
    session.add_all([
        HouseholdUser(household_id=h.id, user_id=saeed.id, role="owner"),
        HouseholdUser(household_id=h.id, user_id=partner.id, role="member"),
    ])
    session.add_all([
        Category(household_id=h.id, name="Groceries", is_income=False),
        Category(household_id=h.id, name="Eating Out", is_income=False),
        Category(household_id=h.id, name="Subscriptions & Entertainment", is_income=False),
    ])
    session.commit()
    return h


CSV_BASIC = (
    "Product,amount,paid_by,category,belongs_to,description,date\n"
    "REMA,500,Saeed,Groceries,,weekly,2026-05-10\n"
    "Bus,40,Saeed,Mat,,,2026-05-11\n"
    "Bus,40,Saeed,mat,,,2026-05-12\n"
    "Coffee,30,Partner,FOOD,,,2026-05-13\n"
)


class TestAutoCreateCategories:
    def test_unknown_category_is_created(self, session: Session, household: Household):
        result = import_csv(session, household.id, CSV_BASIC)
        session.commit()

        assert result.inserted == 4
        assert "Mat" in result.created_categories
        assert "Food" in result.created_categories  # title-cased

        names = {c.name for c in session.query(Category).filter_by(household_id=household.id).all()}
        assert names == {
            "Groceries",
            "Eating Out",
            "Subscriptions & Entertainment",
            "Mat",
            "Food",
        }

    def test_case_variants_dedupe(self, session: Session, household: Household):
        result = import_csv(session, household.id, CSV_BASIC)
        session.commit()
        # 'Mat' and 'mat' should both resolve to the same new category, not two
        mat_count = (
            session.query(Category)
            .filter(Category.household_id == household.id, Category.name == "Mat")
            .count()
        )
        assert mat_count == 1
        # Two transactions point at it
        cat = session.query(Category).filter_by(household_id=household.id, name="Mat").one()
        tx_count = session.query(Transaction).filter_by(category_id=cat.id).count()
        assert tx_count == 2

    def test_disable_auto_create(self, session: Session, household: Household):
        result = import_csv(
            session, household.id, CSV_BASIC, create_missing_categories=False
        )
        session.commit()
        assert result.created_categories == []
        # Mat/Food rows imported as uncategorized
        uncategorized = (
            session.query(Transaction)
            .filter(Transaction.category_id.is_(None))
            .count()
        )
        assert uncategorized == 3

    def test_empty_category_field_stays_uncategorized(
        self, session: Session, household: Household
    ):
        csv = (
            "Product,amount,paid_by,category,belongs_to,description,date\n"
            "Mystery,100,Saeed,,,,2026-05-10\n"
        )
        result = import_csv(session, household.id, csv)
        session.commit()
        assert result.inserted == 1
        assert result.created_categories == []
        tx = session.query(Transaction).first()
        assert tx.category_id is None
        assert tx.needs_review is True

    def test_matches_category_name_without_external_id(
        self, session: Session, household: Household
    ):
        csv = (
            "Product,amount,paid_by,Category,belongs_to,Date\n"
            "Dinner,100,Saeed,Eating Out,,2026-05\n"
            "Netflix,129,Saeed,Subscriptions,,2026-05\n"
        )
        result = import_csv(session, household.id, csv)
        session.commit()

        assert result.inserted == 2
        assert result.created_categories == []
        uncategorized = (
            session.query(Transaction)
            .filter(Transaction.category_id.is_(None))
            .count()
        )
        assert uncategorized == 0
