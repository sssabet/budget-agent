"""Tests for trend / pattern analytics.

These tests pin down: month-over-month windowing, top-merchant ordering,
recurring-subscription detection (the "consistent amount" rule that
distinguishes Netflix from groceries), and the income/Investment exclusions.
"""
from datetime import date
from decimal import Decimal

from app.tools.analytics import (
    find_recurring_subscriptions,
    month_over_month_spend,
    top_merchants,
)
from app.tools.types import Category, Transaction


GROCERIES = Category(id="c1", name="Groceries", is_income=False)
SUBS = Category(id="c2", name="Subscriptions", is_income=False)
INCOME = Category(id="c3", name="Salary", is_income=True)
INVESTMENT = Category(id="c4", name="Investment", is_income=False)


def t(
    amount: str,
    *,
    cat: Category | None = GROCERIES,
    d: date | None = date(2026, 5, 10),
    product: str = "x",
    tid: str = "t",
) -> Transaction:
    return Transaction(
        id=tid,
        date=d,
        date_is_estimated=False,
        product=product,
        amount=Decimal(amount),
        paid_by=None,
        belongs_to=None,
        category=cat,
        description=None,
        needs_review=cat is None,
    )


class TestMonthOverMonth:
    def test_returns_one_row_per_month_in_order(self):
        txs = [
            t("100", d=date(2026, 3, 5), product="a"),
            t("200", d=date(2026, 4, 5), product="b"),
            t("300", d=date(2026, 5, 5), product="c"),
        ]
        rows = month_over_month_spend(
            txs, [date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1)]
        )
        assert [r.month for r in rows] == [
            date(2026, 3, 1),
            date(2026, 4, 1),
            date(2026, 5, 1),
        ]
        assert [r.total_expense for r in rows] == [
            Decimal("100"),
            Decimal("200"),
            Decimal("300"),
        ]

    def test_empty_month_yields_zeroes(self):
        rows = month_over_month_spend([], [date(2026, 5, 1)])
        assert rows[0].total_expense == Decimal("0")
        assert rows[0].total_income == Decimal("0")
        assert rows[0].net == Decimal("0")
        assert rows[0].by_category == {}

    def test_income_separated_from_expense(self):
        txs = [
            t("65000", cat=INCOME, d=date(2026, 5, 1)),
            t("100", cat=GROCERIES, d=date(2026, 5, 1)),
        ]
        rows = month_over_month_spend(txs, [date(2026, 5, 1)])
        assert rows[0].total_income == Decimal("65000")
        assert rows[0].total_expense == Decimal("100")
        assert rows[0].net == Decimal("64900")

    def test_dates_normalized_to_first_of_month(self):
        # Caller passes date(2026, 5, 17); we should normalize to 2026-05-01.
        rows = month_over_month_spend([], [date(2026, 5, 17)])
        assert rows[0].month == date(2026, 5, 1)


class TestTopMerchants:
    def test_orders_by_total_desc(self):
        txs = [
            t("100", product="REMA"),
            t("50", product="REMA"),
            t("200", product="Netflix", cat=SUBS),
            t("75", product="KIWI"),
        ]
        rows = top_merchants(txs)
        names = [r.merchant for r in rows]
        assert names[0] == "Netflix"  # 200
        assert names[1] == "REMA"     # 150
        assert names[2] == "KIWI"     # 75

    def test_collapses_case_and_whitespace(self):
        txs = [
            t("100", product="REMA 1000"),
            t("50", product="rema 1000"),
            t("60", product=" REMA  1000 "),
        ]
        rows = top_merchants(txs)
        assert len(rows) == 1
        assert rows[0].occurrences == 3
        assert rows[0].total == Decimal("210")

    def test_excludes_income_and_investment(self):
        txs = [
            t("65000", cat=INCOME, product="Salary"),
            t("2000", cat=INVESTMENT, product="DNB Index Fund"),
            t("100", product="REMA"),
        ]
        rows = top_merchants(txs)
        assert {r.merchant for r in rows} == {"REMA"}

    def test_month_filter(self):
        txs = [
            t("100", product="REMA", d=date(2026, 5, 5)),
            t("200", product="Netflix", cat=SUBS, d=date(2026, 6, 5)),
        ]
        rows = top_merchants(txs, month=date(2026, 5, 1))
        assert [r.merchant for r in rows] == ["REMA"]

    def test_n_caps_results(self):
        txs = [t(f"{i*10}", product=f"m{i}", tid=f"t{i}") for i in range(1, 6)]
        rows = top_merchants(txs, n=3)
        assert len(rows) == 3

    def test_n_zero_returns_empty(self):
        txs = [t("100", product="REMA")]
        assert top_merchants(txs, n=0) == []

    def test_skips_null_date_when_month_specified(self):
        txs = [t("100", product="REMA", d=None)]
        assert top_merchants(txs, month=date(2026, 5, 1)) == []


class TestRecurringSubscriptions:
    def test_finds_consistent_monthly_charge(self):
        # Netflix at ~129 NOK in three months -> recurring.
        txs = [
            t("129", cat=SUBS, product="Netflix", d=date(2026, 3, 1), tid="a"),
            t("129", cat=SUBS, product="Netflix", d=date(2026, 4, 1), tid="b"),
            t("129", cat=SUBS, product="Netflix", d=date(2026, 5, 1), tid="c"),
        ]
        rows = find_recurring_subscriptions(txs)
        assert len(rows) == 1
        assert rows[0].merchant == "Netflix"
        assert rows[0].months_seen == 3
        assert rows[0].typical_monthly_amount == Decimal("129")
        assert rows[0].category == "Subscriptions"
        assert rows[0].last_seen == date(2026, 5, 1)

    def test_amount_drift_within_tolerance_still_qualifies(self):
        # Default tolerance is 20%. 100 / 110 / 90 around median 100 -> all within 20%.
        txs = [
            t("100", product="Spotify", cat=SUBS, d=date(2026, 3, 1), tid="a"),
            t("110", product="Spotify", cat=SUBS, d=date(2026, 4, 1), tid="b"),
            t("90", product="Spotify", cat=SUBS, d=date(2026, 5, 1), tid="c"),
        ]
        rows = find_recurring_subscriptions(txs)
        assert [r.merchant for r in rows] == ["Spotify"]

    def test_volatile_amounts_excluded(self):
        # Same merchant, wildly different amounts -> not a subscription.
        txs = [
            t("100", product="REMA", d=date(2026, 3, 1), tid="a"),
            t("450", product="REMA", d=date(2026, 4, 1), tid="b"),
            t("80", product="REMA", d=date(2026, 5, 1), tid="c"),
        ]
        assert find_recurring_subscriptions(txs) == []

    def test_min_months_threshold(self):
        # Only 2 months -> below default min_months=3.
        txs = [
            t("129", product="Netflix", cat=SUBS, d=date(2026, 4, 1), tid="a"),
            t("129", product="Netflix", cat=SUBS, d=date(2026, 5, 1), tid="b"),
        ]
        assert find_recurring_subscriptions(txs) == []
        # But min_months=2 should pick it up.
        rows = find_recurring_subscriptions(txs, min_months=2)
        assert [r.merchant for r in rows] == ["Netflix"]

    def test_income_excluded(self):
        txs = [
            t("65000", cat=INCOME, product="Salary", d=date(2026, 3, 1), tid="a"),
            t("65000", cat=INCOME, product="Salary", d=date(2026, 4, 1), tid="b"),
            t("65000", cat=INCOME, product="Salary", d=date(2026, 5, 1), tid="c"),
        ]
        assert find_recurring_subscriptions(txs) == []

    def test_investment_excluded(self):
        txs = [
            t("2000", cat=INVESTMENT, product="DNB Index", d=date(2026, 3, 1), tid="a"),
            t("2000", cat=INVESTMENT, product="DNB Index", d=date(2026, 4, 1), tid="b"),
            t("2000", cat=INVESTMENT, product="DNB Index", d=date(2026, 5, 1), tid="c"),
        ]
        assert find_recurring_subscriptions(txs) == []

    def test_null_dates_excluded(self):
        txs = [
            t("129", cat=SUBS, product="Netflix", d=None, tid="a"),
            t("129", cat=SUBS, product="Netflix", d=None, tid="b"),
            t("129", cat=SUBS, product="Netflix", d=None, tid="c"),
        ]
        assert find_recurring_subscriptions(txs) == []

    def test_ordered_by_amount_desc(self):
        txs = []
        # Netflix 129 monthly
        for i, m in enumerate([3, 4, 5]):
            txs.append(t("129", product="Netflix", cat=SUBS, d=date(2026, m, 1), tid=f"n{i}"))
        # Spotify 99 monthly
        for i, m in enumerate([3, 4, 5]):
            txs.append(t("99", product="Spotify", cat=SUBS, d=date(2026, m, 1), tid=f"s{i}"))
        rows = find_recurring_subscriptions(txs)
        assert [r.merchant for r in rows] == ["Netflix", "Spotify"]
