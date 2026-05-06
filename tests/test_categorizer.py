"""Tests for the merchant-rules categorizer.

The agent's job here is to propose, not to apply. Tests pin down: rule matching,
case insensitivity, household-scoped resolution (don't suggest categories the
household doesn't own), and the month filter.
"""
from datetime import date
from decimal import Decimal

from app.tools.categorizer import (
    MERCHANT_RULES,
    propose_categories,
    propose_for_transaction,
)
from app.tools.types import Category, Transaction


GROCERY = Category(id="c1", name="Groceries", is_income=False)
TRANSPORT = Category(id="c2", name="Transport", is_income=False)


def t(
    product: str,
    *,
    cat: Category | None = None,
    description: str | None = None,
    d: date | None = date(2026, 5, 10),
    tid: str = "t1",
) -> Transaction:
    return Transaction(
        id=tid,
        date=d,
        date_is_estimated=False,
        product=product,
        amount=Decimal("100"),
        paid_by=None,
        belongs_to=None,
        category=cat,
        description=description,
        needs_review=cat is None,
    )


class TestProposeForTransaction:
    def test_matches_substring_in_product(self):
        # The household's category name uses different casing — resolution
        # should be case-insensitive but preserve the household's spelling.
        avail = {"groceries": "Groceries"}
        s = propose_for_transaction(t("REMA 1000 Storo"), avail)
        assert s is not None
        assert s.suggested_category == "Groceries"
        assert "rema" in s.reason

    def test_falls_back_to_description(self):
        avail = {"transport": "Transport"}
        s = propose_for_transaction(t("ukjent kjøp", description="Ruter månedskort"), avail)
        assert s is not None
        assert s.suggested_category == "Transport"

    def test_returns_none_when_no_rule_matches(self):
        avail = {"groceries": "Groceries"}
        assert propose_for_transaction(t("Some unknown shop"), avail) is None

    def test_already_categorized_returns_none(self):
        avail = {"groceries": "Groceries"}
        assert propose_for_transaction(t("REMA", cat=GROCERY), avail) is None

    def test_skips_rule_when_household_lacks_category(self):
        # Rule fires on "netflix" -> Subscriptions & Entertainment, but if the
        # household doesn't have that category the suggestion should be skipped
        # rather than inventing a name.
        avail = {"groceries": "Groceries"}
        assert propose_for_transaction(t("Netflix"), avail) is None

    def test_empty_text_returns_none(self):
        avail = {"groceries": "Groceries"}
        assert propose_for_transaction(t(""), avail) is None


class TestProposeCategories:
    def test_returns_only_uncategorized(self):
        avail = ["Groceries", "Transport"]
        txs = [
            t("REMA 1000", tid="a"),
            t("Ruter", tid="b"),
            t("REMA 1000", cat=GROCERY, tid="c"),  # already categorized
        ]
        out = propose_categories(txs, avail)
        ids = {s.transaction_id for s in out}
        assert ids == {"a", "b"}

    def test_month_filter(self):
        avail = ["Groceries"]
        txs = [
            t("REMA may", d=date(2026, 5, 10), tid="m"),
            t("REMA june", d=date(2026, 6, 10), tid="j"),
        ]
        out = propose_categories(txs, avail, month=date(2026, 5, 1))
        ids = {s.transaction_id for s in out}
        assert ids == {"m"}

    def test_month_filter_excludes_null_date(self):
        # Without a date we don't know which month it belongs to. Don't pretend.
        avail = ["Groceries"]
        txs = [t("REMA", d=None, tid="x")]
        assert propose_categories(txs, avail, month=date(2026, 5, 1)) == []

    def test_no_month_includes_null_date(self):
        avail = ["Groceries"]
        txs = [t("REMA", d=None, tid="x")]
        out = propose_categories(txs, avail, month=None)
        assert len(out) == 1
        assert out[0].transaction_id == "x"


class TestRulesTable:
    def test_all_rule_keys_are_lowercase(self):
        # The matcher lowercases the haystack, so rule keys must be lowercase
        # or they will silently never fire.
        for key in MERCHANT_RULES:
            assert key == key.lower(), f"rule key {key!r} must be lowercase"
