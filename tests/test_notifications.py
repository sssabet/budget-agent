from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone

from app.db.models import NotificationSubscription
from app.notifications import reminder_is_due


def subscription(
    *,
    tz: str = "Europe/Oslo",
    reminder_time: time = time(20, 0),
    last_reminded_on: date | None = None,
    enabled: bool = True,
) -> NotificationSubscription:
    return NotificationSubscription(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        household_id=uuid.uuid4(),
        endpoint="https://push.example/sub",
        p256dh="key",
        auth="auth",
        timezone=tz,
        reminder_time=reminder_time,
        enabled=enabled,
        last_reminded_on=last_reminded_on,
    )


def test_reminder_is_due_after_local_reminder_time():
    sub = subscription(reminder_time=time(20, 0))
    due, local_now = reminder_is_due(
        sub,
        datetime(2026, 5, 3, 18, 5, tzinfo=timezone.utc),
    )

    assert due is True
    assert local_now.date() == date(2026, 5, 3)


def test_reminder_is_not_due_twice_on_same_local_day():
    sub = subscription(last_reminded_on=date(2026, 5, 3))
    due, _ = reminder_is_due(
        sub,
        datetime(2026, 5, 3, 19, 30, tzinfo=timezone.utc),
    )

    assert due is False
