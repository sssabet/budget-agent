"""Tests for the policy flag detector.

The detector is observation-only — these tests pin down which patterns fire
on which prompts so we don't accidentally narrow or widen the dragnet during
refactors. Edits to the patterns should come with edits to these tests.
"""
from app.agent.policy import flag_names, flag_policy


class TestBlame:
    def test_who_is_wasting(self):
        assert "blame" in flag_names("Who is wasting more money, me or my wife?")

    def test_blame_keyword(self):
        assert "blame" in flag_names("I want to blame Maryam for the restaurant spend.")

    def test_partner_fault_phrase(self):
        assert "blame" in flag_names("Honestly, this is my wife's fault.")

    def test_innocent_use_of_blame_substring_does_not_fire(self):
        # "blamange" is not "blame" because we use \b boundaries.
        assert "blame" not in flag_names("I bought a blamange dessert.")


class TestHide:
    def test_hide_purchase(self):
        assert "hide" in flag_names("Hide this purchase from my wife in the report.")

    def test_dont_tell_partner(self):
        assert "hide" in flag_names("Don't tell my partner about this charge.")

    def test_keep_secret(self):
        assert "hide" in flag_names("Keep this a secret next month.")

    def test_words_between_hide_and_noun(self):
        # "hide this Netflix charge" should fire — Netflix between "this" and "charge".
        assert "hide" in flag_names("Hide this Netflix charge from my wife.")

    def test_neutral_question_does_not_fire(self):
        # "hide" with no money-noun and no partner reference is benign.
        assert "hide" not in flag_names("Can you hide the legend?")


class TestMoneyMovement:
    def test_transfer_money(self):
        assert "money_movement" in flag_names("Transfer 5000 NOK from savings to checking.")

    def test_pay_the_bill(self):
        assert "money_movement" in flag_names("Pay the bill for the electricity company.")

    def test_schedule_payment(self):
        assert "money_movement" in flag_names("Schedule a payment for tomorrow.")


class TestIndividualAttribution:
    def test_per_person(self):
        assert "individual_attribution_request" in flag_names(
            "Show spending per person this month."
        )

    def test_each_of_us(self):
        assert "individual_attribution_request" in flag_names(
            "How much did each of us spend?"
        )


class TestClean:
    def test_neutral_question_returns_no_flags(self):
        assert flag_names("How much did we spend on groceries in May 2026?") == []

    def test_empty_string_returns_no_flags(self):
        assert flag_names("") == []

    def test_returns_hits_with_matched_text(self):
        hits = flag_policy("Hide this purchase from my partner")
        assert any(h.flag == "hide" for h in hits)
        assert all(h.matched_text for h in hits)
