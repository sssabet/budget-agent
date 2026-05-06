"""CSV → DB importer.

Expected columns (case-insensitive, extras are ignored):
  Product, amount, paid_by, category, belongs_to, description, date

Forgiving by design:
- date can be empty -> stored as null
- date can be year-month only ("2026-04") -> stored as 2026-04-01 with date_is_estimated=true
- paid_by / belongs_to are matched against household member display_name (case-insensitive); unknown -> null
- category unknown -> transaction stored uncategorized + needs_review=true
- a row that fails to parse goes into the rejected list with a reason; the rest still import
"""
from __future__ import annotations

import csv
import io
import re
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import IO

from sqlalchemy.orm import Session

from app.db import models as m

YEAR_MONTH_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})\s*$")
FULL_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

CATEGORY_ALIASES = {
    "subscriptions": "subscriptionsentertainment",
    "entertainment": "subscriptionsentertainment",
    "entertaiment": "subscriptionsentertainment",
    "grocery": "groceries",
    "travelling": "travel",
}


@dataclass
class ImportResult:
    inserted: int
    rejected: list[tuple[int, str, str]]  # row index (1-based), reason, raw row repr
    created_categories: list[str]  # category names freshly created by this import


def _parse_date(raw: str | None) -> tuple[date | None, bool]:
    """Returns (date, date_is_estimated). Empty/None -> (None, False)."""
    if raw is None or not raw.strip():
        return None, False
    full = FULL_DATE_RE.match(raw)
    if full:
        y, mo, d = (int(g) for g in full.groups())
        return date(y, mo, d), False
    ym = YEAR_MONTH_RE.match(raw)
    if ym:
        y, mo = (int(g) for g in ym.groups())
        return date(y, mo, 1), True
    raise ValueError(f"unrecognized date format: {raw!r}")


def _parse_amount(raw: str) -> Decimal:
    if raw is None or not raw.strip():
        raise ValueError("amount is required")
    cleaned = raw.replace(" ", "").replace(",", ".")
    if cleaned.startswith("-"):
        cleaned = cleaned[1:]
    try:
        d = Decimal(cleaned)
    except InvalidOperation as e:
        raise ValueError(f"unparseable amount: {raw!r}") from e
    if d <= 0:
        raise ValueError(f"amount must be positive: {raw!r}")
    return d


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _norm_category(s: str | None) -> str:
    compact = NON_ALNUM_RE.sub("", _norm(s))
    return CATEGORY_ALIASES.get(compact, compact)


def _titlecase(name: str) -> str:
    """Normalize category names so 'food' / 'FOOD' / 'Food' all become 'Food'.
    First-letter-of-each-word is good enough; the user can rename later."""
    return " ".join(w[:1].upper() + w[1:].lower() for w in name.strip().split())


def import_csv(
    s: Session,
    household_id: uuid.UUID,
    csv_input: IO[str] | str,
    *,
    create_missing_categories: bool = True,
) -> ImportResult:
    """Import rows from an open file-like or a CSV string.

    create_missing_categories=True (default): when a CSV row has a category name
    we don't recognize (e.g. "Mat" when the household only has "Groceries"), we
    create a new Category for the household instead of silently leaving the row
    uncategorized. The CSV is the user's source of truth; the DB should learn
    from it.
    """
    if isinstance(csv_input, str):
        csv_input = io.StringIO(csv_input)

    reader = csv.DictReader(csv_input)

    # build lookup maps for this household
    members = {
        _norm(hu.user.display_name): hu.user
        for hu in s.query(m.HouseholdUser).filter_by(household_id=household_id).all()
    }
    cats: dict[str, m.Category] = {
        _norm_category(c.name): c
        for c in s.query(m.Category).filter_by(household_id=household_id).all()
    }

    inserted = 0
    rejected: list[tuple[int, str, str]] = []
    created_categories: list[str] = []

    # DictReader normalizes nothing — let us be case-insensitive on headers
    for i, row_raw in enumerate(reader, start=1):
        row = {(k or "").strip().lower(): (v or "") for k, v in row_raw.items()}
        try:
            product = row.get("product", "").strip()
            if not product:
                raise ValueError("product is required")

            amount = _parse_amount(row.get("amount", ""))
            d, est = _parse_date(row.get("date"))

            paid_by_user = members.get(_norm(row.get("paid_by")))
            belongs_to_user = members.get(_norm(row.get("belongs_to")))

            cat_raw = (
                row.get("category")
                or row.get("category_name")
                or row.get("category_id")
                or ""
            ).strip()
            cat: m.Category | None = None
            if cat_raw:
                cat = cats.get(_norm_category(cat_raw))
                if cat is None and create_missing_categories:
                    pretty = _titlecase(cat_raw)
                    cat = m.Category(
                        household_id=household_id, name=pretty, is_income=False
                    )
                    s.add(cat)
                    s.flush()  # need the id for subsequent rows in this import
                    cats[_norm_category(pretty)] = cat
                    created_categories.append(pretty)

            tx = m.Transaction(
                household_id=household_id,
                date=d,
                date_is_estimated=est,
                product=product,
                amount=amount,
                paid_by_user_id=paid_by_user.id if paid_by_user else None,
                belongs_to_user_id=belongs_to_user.id if belongs_to_user else None,
                category_id=cat.id if cat else None,
                description=(row.get("description") or "").strip() or None,
                needs_review=cat is None,
            )
            s.add(tx)
            inserted += 1
        except Exception as e:  # pragma: no cover - we want all parse errors collected
            rejected.append((i, str(e), str(row_raw)))

    return ImportResult(
        inserted=inserted, rejected=rejected, created_categories=created_categories
    )
