from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.api.auth import AuthenticatedUser, authorized_household


def user_with_households() -> tuple[AuthenticatedUser, uuid.UUID, uuid.UUID]:
    first = uuid.uuid4()
    second = uuid.uuid4()
    return (
        AuthenticatedUser(
            id=uuid.uuid4(),
            email="saeed@example.com",
            display_name="Saeed",
            households=((first, "Home"), (second, "Cabin")),
        ),
        first,
        second,
    )


class TestAuthorizedHousehold:
    def test_defaults_to_first_household(self):
        user, first, _ = user_with_households()

        assert authorized_household(user, None) == (first, "Home")

    def test_allows_named_member_household(self):
        user, _, second = user_with_households()

        assert authorized_household(user, "Cabin") == (second, "Cabin")

    def test_rejects_household_outside_membership(self):
        user, _, _ = user_with_households()

        with pytest.raises(HTTPException) as exc:
            authorized_household(user, "Someone else")

        assert exc.value.status_code == 403
