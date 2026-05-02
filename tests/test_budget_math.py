"""Tests for the deterministic math layer.

These tests are intentionally boring. Boring math is where a budget agent either
becomes useful or starts lying with confidence.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.tools.budget_math import (
    compare_budget_vs_actual,
    list_uncategorized,
    spend_by_category,
    spend_by_owner,
    summarize_month,
    total_expense,
    total_income,
)
from app.tools.types import Budget, Category, Transaction

GROCERIES = Category(id="c1", name="Groceries", is_income=False)
RESTAURANTS = Category(id="c2", name="Restaurants", is_income=False)
TRANSPORT = Category(id="c3", name="Transport", is_income=False)
INCOME = Category(id="c4", name="Salary", is_income=True)
INVESTMENT = Category(id="c5", name="Investment", is_income=False)

CATS_BY_ID = {c.id: c.name for c in [GROCERIES, RESTAURANTS, TRANSPORT, INCOME, INVESTMENT]}

MAY = date(2026, 5, 1)
JUNE = date(2026, 6, 1)


def t(
    amount: str,
    *,
    cat: Category | None = GROCERIES,
    d: date | None = date(2026, 5, 10),
    estimated: bool = False,
    paid_by: str | None = None,
    belongs_to: str | None = None,
    product: str = "x",
    review: bool = False,
) -> Transaction:
    return Transaction(
        id="t-" + product + str(amount),
        date=d,
        date_is_estimated=estimated,
        product=product,
        amount=Decimal(amount),
        paid_by=paid_by,
        belongs_to=belongs_to,
        category=cat,
        description=None,
        needs_review=review,
    )


class TestSpendByCategory:
    def test_groups_by_category_for_the_month(self):
        txs = [
            t("100", cat=GROCERIES),
            t("50", cat=GROCERIES),
            t("200", cat=RESTAURANTS),
        ]
        result = spend_by_category(txs, MAY)
        assert result == {"Groceries": Decimal("150"), "Restaurants": Decimal("200")}

    def test_excludes_other_months(self):
        txs = [
            t("100", cat=GROCERIES, d=date(2026, 5, 10)),
            t("999", cat=GROCERIES, d=date(2026, 6, 10)),
        ]
        assert spend_by_category(txs, MAY) == {"Groceries": Decimal("100")}

    def test_excludes_income(self):
        txs = [t("65000", cat=INCOME), t("100", cat=GROCERIES)]
        assert spend_by_category(txs, MAY) == {"Groceries": Decimal("100")}

    def test_uncategorized_bucket(self):
        txs = [t("100", cat=None)]
        assert spend_by_category(txs, MAY) == {"(uncategorized)": Decimal("100")}

    def test_skips_transactions_with_null_date(self):
        # Null date = we genuinely don't know when. Don't guess into a month.
        txs = [t("100", d=None)]
        assert spend_by_category(txs, MAY) == {}

    def test_estimated_dates_still_counted(self):
        # date_is_estimated=true means "we know the month, just not the day".
        # Should still aggregate into the right month.
        txs = [t("100", d=date(2026, 5, 1), estimated=True)]
        assert spend_by_category(txs, MAY) == {"Groceries": Decimal("100")}


class TestIncomeAndExpense:
    def test_income_isolated_from_expense(self):
        txs = [t("65000", cat=INCOME), t("100", cat=GROCERIES), t("50", cat=RESTAURANTS)]
        assert total_income(txs, MAY) == Decimal("65000")
        assert total_expense(txs, MAY) == Decimal("150")

    def test_uncategorized_counts_as_expense(self):
        # Conservative default: unknown transactions are spending until proven otherwise.
        txs = [t("100", cat=None)]
        assert total_expense(txs, MAY) == Decimal("100")
        assert total_income(txs, MAY) == Decimal("0")

    def test_investment_is_outflow_but_not_cost(self):
        txs = [t("2000", cat=INVESTMENT), t("100", cat=GROCERIES)]
        assert spend_by_category(txs, MAY) == {"Groceries": Decimal("100")}
        assert total_expense(txs, MAY) == Decimal("100")
        assert total_income(txs, MAY) == Decimal("0")


class TestCompareBudgetVsActual:
    def test_status_classifications(self):
        txs = [
            t("8000", cat=GROCERIES),     # over (budget 7000)
            t("2700", cat=RESTAURANTS),   # near (budget 3000, ~90%)
            t("500", cat=TRANSPORT),      # under (budget 2500)
        ]
        budgets = [
            Budget(month=MAY, category_id="c1", amount=Decimal("7000")),
            Budget(month=MAY, category_id="c2", amount=Decimal("3000")),
            Budget(month=MAY, category_id="c3", amount=Decimal("2500")),
        ]
        reports = {r.category_name: r for r in compare_budget_vs_actual(txs, budgets, CATS_BY_ID, MAY)}
        assert reports["Groceries"].status == "over"
        assert reports["Groceries"].variance == Decimal("-1000")
        assert reports["Restaurants"].status == "near"
        assert reports["Transport"].status == "under"

    def test_unbudgeted_spend_reports_over(self):
        # Spending in a category with no budget is conceptually "over budget" — call it out.
        txs = [t("100", cat=RESTAURANTS)]
        reports = compare_budget_vs_actual(txs, [], CATS_BY_ID, MAY)
        assert len(reports) == 1
        assert reports[0].category_name == "Restaurants"
        assert reports[0].status == "over"
        assert reports[0].budgeted == Decimal("0")

    def test_budget_with_no_spend_reports_under(self):
        budgets = [Budget(month=MAY, category_id="c1", amount=Decimal("1000"))]
        reports = compare_budget_vs_actual([], budgets, CATS_BY_ID, MAY)
        assert reports[0].status == "under"
        assert reports[0].variance == Decimal("1000")


class TestSpendByOwner:
    def test_household_default_when_belongs_to_null(self):
        txs = [
            t("100", cat=GROCERIES, belongs_to=None),
            t("200", cat=RESTAURANTS, belongs_to="Wife"),
            t("50", cat=GROCERIES, belongs_to="Saeed"),
        ]
        result = spend_by_owner(txs, MAY)
        assert result == {
            "Household": Decimal("100"),
            "Wife": Decimal("200"),
            "Saeed": Decimal("50"),
        }

    def test_investment_excluded_from_owner_spend(self):
        txs = [
            t("100", cat=GROCERIES, belongs_to=None),
            t("2000", cat=INVESTMENT, belongs_to=None),
        ]
        assert spend_by_owner(txs, MAY) == {"Household": Decimal("100")}


class TestListUncategorized:
    def test_returns_only_uncategorized_in_month(self):
        txs = [
            t("100", cat=None, d=date(2026, 5, 10), product="?"),
            t("200", cat=GROCERIES),
            t("300", cat=None, d=date(2026, 6, 10), product="?2"),
        ]
        out = list_uncategorized(txs, MAY)
        assert len(out) == 1
        assert out[0].amount == Decimal("100")


class TestSummarizeMonth:
    def test_full_summary(self):
        txs = [
            t("65000", cat=INCOME),
            t("8000", cat=GROCERIES),
            t("2000", cat=RESTAURANTS),
            t("500", cat=None, product="mystery"),
            t("100", cat=GROCERIES, estimated=True),
        ]
        budgets = [
            Budget(month=MAY, category_id="c1", amount=Decimal("7000")),
            Budget(month=MAY, category_id="c2", amount=Decimal("3000")),
        ]
        s = summarize_month(txs, budgets, CATS_BY_ID, MAY)
        assert s.total_income == Decimal("65000")
        assert s.total_expense == Decimal("10600")
        assert s.net == Decimal("54400")
        assert s.uncategorized_count == 1
        assert s.estimated_date_count == 1
        assert s.over_budget_categories[0].category_name == "Groceries"
