from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db import models as m

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueReminder:
    subscription: m.NotificationSubscription
    local_now: datetime


def reminder_is_due(
    subscription: m.NotificationSubscription,
    now_utc: datetime,
) -> tuple[bool, datetime]:
    try:
        local_now = now_utc.astimezone(ZoneInfo(subscription.timezone))
    except ZoneInfoNotFoundError:
        local_now = now_utc.astimezone(timezone.utc)

    already_sent_today = subscription.last_reminded_on == local_now.date()
    is_after_reminder_time = local_now.time() >= subscription.reminder_time
    return subscription.enabled and is_after_reminder_time and not already_sent_today, local_now


def due_reminders(
    subscriptions: list[m.NotificationSubscription],
    now_utc: datetime,
) -> list[DueReminder]:
    out: list[DueReminder] = []
    for sub in subscriptions:
        due, local_now = reminder_is_due(sub, now_utc)
        if due:
            out.append(DueReminder(subscription=sub, local_now=local_now))
    return out


def send_daily_reminder(
    subscription: m.NotificationSubscription,
    *,
    vapid_private_key: str,
    vapid_subject: str,
) -> None:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        raise RuntimeError("pywebpush is required to send Web Push notifications") from exc

    payload = {
        "title": "Budget check-in",
        "body": "Have you entered today's expenses?",
        "url": "/#add",
    }
    subscription_info = {
        "endpoint": subscription.endpoint,
        "keys": {
            "p256dh": subscription.p256dh,
            "auth": subscription.auth,
        },
    }

    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=vapid_private_key,
            vapid_claims={"sub": vapid_subject},
        )
    except WebPushException:
        log.exception("failed to send reminder push", extra={"subscription_id": str(subscription.id)})
        raise
