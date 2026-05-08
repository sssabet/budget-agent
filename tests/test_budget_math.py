"""Tests for the deterministic math layer.

These tests are intentionally boring. Boring math is where a budget agent either
becomes useful or starts lying with confidence.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.tools.budget_math import (
    compare_budget_vs_actual,
    compute_planning_baseline,
    diff_budget,
    list_uncategorized,
    spend_by_category,
    spend_by_owner,
    suggest_allocations,
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


def _t(amount: str, *, cat: Category | None, d: date, product: str = "x") -> Transaction:
    return Transaction(
        id=f"t-{product}-{d.isoformat()}-{amount}",
        date=d,
        date_is_estimated=False,
        product=product,
        amount=Decimal(amount),
        paid_by=None,
        belongs_to=None,
        category=cat,
        description=None,
        needs_review=False,
    )


JAN = date(2026, 1, 1)
FEB = date(2026, 2, 1)
MAR = date(2026, 3, 1)
APR = date(2026, 4, 1)


class TestComputePlanningBaseline:
    def test_zero_fills_inactive_months(self):
        # Groceries seen only in 2 of 3 months → avg uses zero-fill.
        txs = [
            _t("3000", cat=GROCERIES, d=JAN),
            _t("3000", cat=GROCERIES, d=MAR),
            _t("65000", cat=INCOME, d=JAN),
            _t("65000", cat=INCOME, d=FEB),
            _t("65000", cat=INCOME, d=MAR),
        ]
        baseline = compute_planning_baseline(txs, [JAN, FEB, MAR])
        groceries = next(c for c in baseline.categories if c.category_name == "Groceries")
        assert groceries.months_observed == 2
        assert groceries.avg_monthly == Decimal("2000")  # 6000 / 3, not / 2
        assert groceries.last_month == Decimal("3000")
        assert baseline.avg_monthly_income == Decimal("65000")

    def test_recurring_floor_only_with_three_observations(self):
        # 2 observations: not enough signal — floor stays 0.
        txs_two = [
            _t("199", cat=RESTAURANTS, d=JAN),
            _t("199", cat=RESTAURANTS, d=FEB),
        ]
        b2 = compute_planning_baseline(txs_two, [JAN, FEB, MAR])
        rest = next(c for c in b2.categories if c.category_name == "Restaurants")
        assert rest.recurring_floor == Decimal("0")

        # 3+ observations: floor = min observed.
        txs_three = txs_two + [_t("250", cat=RESTAURANTS, d=MAR)]
        b3 = compute_planning_baseline(txs_three, [JAN, FEB, MAR])
        rest3 = next(c for c in b3.categories if c.category_name == "Restaurants")
        assert rest3.recurring_floor == Decimal("199")

    def test_empty_window_returns_zeros(self):
        baseline = compute_planning_baseline([], [])
        assert baseline.categories == []
        assert baseline.avg_monthly_income == Decimal("0")
        assert baseline.avg_monthly_expense == Decimal("0")


class TestSuggestAllocations:
    def _baseline(self) -> object:
        # Groceries 6000 avg, Restaurants 2000 avg, Transport 1000 avg, Income 65000.
        txs = [
            _t("6000", cat=GROCERIES, d=JAN),
            _t("6000", cat=GROCERIES, d=FEB),
            _t("6000", cat=GROCERIES, d=MAR),
            _t("2000", cat=RESTAURANTS, d=JAN),
            _t("2000", cat=RESTAURANTS, d=FEB),
            _t("2000", cat=RESTAURANTS, d=MAR),
            _t("1000", cat=TRANSPORT, d=JAN),
            _t("1000", cat=TRANSPORT, d=FEB),
            _t("1000", cat=TRANSPORT, d=MAR),
            _t("65000", cat=INCOME, d=JAN),
            _t("65000", cat=INCOME, d=FEB),
            _t("65000", cat=INCOME, d=MAR),
        ]
        return compute_planning_baseline(txs, [JAN, FEB, MAR])

    def test_keep_copies_current_budget(self):
        baseline = self._baseline()
        current = {"Groceries": Decimal("7000"), "Restaurants": Decimal("3000")}
        proposal = suggest_allocations(
            baseline,
            target_month=APR,
            current_budget=current,
            strategy="keep",
        )
        assert proposal.allocations == current
        assert proposal.feasibility == "fits"

    def test_keep_falls_back_to_last_month_when_no_current(self):
        baseline = self._baseline()
        proposal = suggest_allocations(
            baseline,
            target_month=APR,
            current_budget={},
            strategy="keep",
        )
        assert proposal.allocations["Groceries"] == Decimal("6000")
        assert any("last month" in n for n in proposal.notes)

    def test_rolling_average_uses_avg_rounded_to_100(self):
        baseline = self._baseline()
        proposal = suggest_allocations(
            baseline,
            target_month=APR,
            current_budget={},
            strategy="rolling_average",
        )
        assert proposal.allocations == {
            "Groceries": Decimal("6000"),
            "Restaurants": Decimal("2000"),
            "Transport": Decimal("1000"),
        }

    def test_adjust_applies_signed_deltas(self):
        baseline = self._baseline()
        current = {
            "Groceries": Decimal("7000"),
            "Restaurants": Decimal("3000"),
            "Transport": Decimal("1500"),
        }
        proposal = suggest_allocations(
            baseline,
            target_month=APR,
            current_budget=current,
            strategy="adjust",
            adjustments={
                "Groceries": Decimal("-1000"),
                "Restaurants": Decimal("500"),
            },
        )
        assert proposal.allocations["Groceries"] == Decimal("6000")
        assert proposal.allocations["Restaurants"] == Decimal("3500")
        assert proposal.allocations["Transport"] == Decimal("1500")  # untouched

    def test_adjust_clamps_negative_to_zero(self):
        baseline = self._baseline()
        current = {"Groceries": Decimal("1000")}
        proposal = suggest_allocations(
            baseline,
            target_month=APR,
            current_budget=current,
            strategy="adjust",
            adjustments={"Groceries": Decimal("-5000")},
        )
        # Clamped at 0 — and dropped from output because no current value left
        # to preserve. (Current was non-zero; we keep the explicit 0 to show
        # the user it was considered.)
        assert proposal.allocations["Groceries"] == Decimal("0")

    def test_savings_target_overshoot_is_flagged(self):
        baseline = self._baseline()
        current = {"Groceries": Decimal("60000")}  # absurd to force overshoot
        proposal = suggest_allocations(
            baseline,
            target_month=APR,
            current_budget=current,
            strategy="keep",
            savings_target=Decimal("10000"),
        )
        assert proposal.feasibility == "overshoots"
        assert proposal.gap < Decimal("0")

    def test_unknown_strategy_raises(self):
        baseline = self._baseline()
        with pytest.raises(ValueError):
            suggest_allocations(
                baseline,
                target_month=APR,
                current_budget={},
                strategy="bogus",
            )


class TestDiffBudget:
    def test_largest_change_first(self):
        current = {"Groceries": Decimal("7000"), "Transport": Decimal("1500")}
        proposed = {"Groceries": Decimal("6000"), "Transport": Decimal("1500"), "Gifts": Decimal("500")}
        rows = diff_budget(current, proposed)
        assert rows[0].category_name == "Groceries"
        assert rows[0].delta == Decimal("-1000")
        # Transport (delta 0) sorts after Gifts (delta +500).
        names_in_order = [r.category_name for r in rows]
        assert names_in_order.index("Gifts") < names_in_order.index("Transport")


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
