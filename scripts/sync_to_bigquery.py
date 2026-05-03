"""Sync the household's data from Postgres into BigQuery for analytics.

Why BigQuery if Postgres already has the data?
Postgres is the OLTP system of record (writes from the UI and the agent).
BigQuery is the analytics path: Looker Studio dashboards, ad-hoc SQL,
joining with external data later. The agent itself uses pure-Python analytics
(`app/tools/analytics.py`) — it doesn't need BQ. This script exists so the
*humans* in the loop (and future Looker dashboards) have a clean analytical
copy.

Strategy: full refresh, single household per run. Volumes are tiny (a couple
years of household transactions = thousands of rows), so simple beats
incremental. Each run truncates and re-inserts. If volumes ever justify CDC,
look at Datastream from Cloud SQL → BigQuery.

Usage:
  python -m scripts.sync_to_bigquery                          # uses env defaults
  python -m scripts.sync_to_bigquery --dataset my_household
  python -m scripts.sync_to_bigquery --dry-run                # show what would land
  python -m scripts.sync_to_bigquery --household 'Some Other Household'

Env:
  GOOGLE_CLOUD_PROJECT  (required)
  BUDGET_AGENT_BQ_DATASET  (default: budget_agent)
  GOOGLE_CLOUD_LOCATION (default: us-central1; BQ uses 'US' or 'EU' multi-regions
                          for actual storage location, set via --bq-location)

Requires `gcloud auth application-default login` and roles/bigquery.dataEditor
on the project (the user already has this — see memory).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from app.config import settings
from app.db.repository import (
    get_household_by_name,
    list_all_transactions_dto,
    list_budgets,
    list_categories,
)
from app.db.session import session_scope


# ----- table schemas -----
# Schemas declared as plain dicts so we can print them in --dry-run without
# importing google-cloud-bigquery on the dry-run code path.

CATEGORIES_SCHEMA = [
    {"name": "household_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "household_name", "type": "STRING", "mode": "REQUIRED"},
    {"name": "category_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "category_name", "type": "STRING", "mode": "REQUIRED"},
    {"name": "is_income", "type": "BOOL", "mode": "REQUIRED"},
    {"name": "synced_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
]

BUDGETS_SCHEMA = [
    {"name": "household_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "household_name", "type": "STRING", "mode": "REQUIRED"},
    {"name": "month", "type": "DATE", "mode": "REQUIRED"},
    {"name": "category_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "amount_nok", "type": "NUMERIC", "mode": "REQUIRED"},
    {"name": "synced_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
]

# Denormalized for analytics convenience: category name and partner names are
# duplicated into the row so dashboards don't need joins.
TRANSACTIONS_SCHEMA = [
    {"name": "household_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "household_name", "type": "STRING", "mode": "REQUIRED"},
    {"name": "transaction_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "transaction_date", "type": "DATE", "mode": "NULLABLE"},
    {"name": "date_is_estimated", "type": "BOOL", "mode": "REQUIRED"},
    {"name": "product", "type": "STRING", "mode": "REQUIRED"},
    {"name": "amount_nok", "type": "NUMERIC", "mode": "REQUIRED"},
    {"name": "category_id", "type": "STRING", "mode": "NULLABLE"},
    {"name": "category_name", "type": "STRING", "mode": "NULLABLE"},
    {"name": "is_income", "type": "BOOL", "mode": "NULLABLE"},
    {"name": "paid_by", "type": "STRING", "mode": "NULLABLE"},
    {"name": "belongs_to", "type": "STRING", "mode": "NULLABLE"},
    {"name": "description", "type": "STRING", "mode": "NULLABLE"},
    {"name": "needs_review", "type": "BOOL", "mode": "REQUIRED"},
    {"name": "synced_at", "type": "TIMESTAMP", "mode": "REQUIRED"},
]


@dataclass
class SyncPayload:
    household_id: str
    household_name: str
    categories: list[dict[str, Any]]
    budgets: list[dict[str, Any]]
    transactions: list[dict[str, Any]]


def _to_decimal_str(v: Decimal) -> str:
    # BQ NUMERIC accepts string; this avoids float-precision surprises.
    return f"{v:.2f}"


def _build_payload(household_name: str, synced_at: datetime) -> SyncPayload:
    with session_scope() as s:
        h = get_household_by_name(s, household_name)
        if h is None:
            raise SystemExit(f"household '{household_name}' not found in Postgres")
        hid = str(h.id)
        cats = list_categories(s, h.id)
        budgets = list_budgets(s, h.id)
        txs = list_all_transactions_dto(s, h.id)
        tx_rows = []
        for t in txs:
            tx_rows.append(
                {
                    "household_id": hid,
                    "household_name": h.name,
                    "transaction_id": t.id,
                    "transaction_date": t.date.isoformat() if t.date else None,
                    "date_is_estimated": bool(t.date_is_estimated),
                    "product": t.product,
                    "amount_nok": _to_decimal_str(t.amount),
                    "category_id": t.category.id if t.category else None,
                    "category_name": t.category.name if t.category else None,
                    "is_income": t.category.is_income if t.category else None,
                    "paid_by": t.paid_by,
                    "belongs_to": t.belongs_to,
                    "description": t.description,
                    "needs_review": bool(t.needs_review),
                    "synced_at": synced_at.isoformat(),
                }
            )

    cat_rows = [
        {
            "household_id": hid,
            "household_name": h.name if False else household_name,  # name carried below
            "category_id": c.id,
            "category_name": c.name,
            "is_income": bool(c.is_income),
            "synced_at": synced_at.isoformat(),
        }
        for c in cats
    ]
    # Re-stamp household_name now that we're outside the session block.
    for r in cat_rows:
        r["household_name"] = household_name

    budget_rows = [
        {
            "household_id": hid,
            "household_name": household_name,
            "month": b.month.isoformat() if isinstance(b.month, date) else str(b.month),
            "category_id": b.category_id,
            "amount_nok": _to_decimal_str(b.amount),
            "synced_at": synced_at.isoformat(),
        }
        for b in budgets
    ]

    return SyncPayload(
        household_id=hid,
        household_name=household_name,
        categories=cat_rows,
        budgets=budget_rows,
        transactions=tx_rows,
    )


def _ensure_table(client, dataset_ref, table_name: str, schema: list[dict[str, Any]]):
    from google.cloud import bigquery

    bq_schema = [
        bigquery.SchemaField(f["name"], f["type"], mode=f["mode"]) for f in schema
    ]
    table_id = f"{dataset_ref}.{table_name}"
    try:
        return client.get_table(table_id)
    except Exception:
        table = bigquery.Table(table_id, schema=bq_schema)
        return client.create_table(table)


def _sync_table(client, dataset_ref, name: str, schema, rows: Iterable[dict[str, Any]]):
    from google.cloud import bigquery

    table = _ensure_table(client, dataset_ref, name, schema)
    rows = list(rows)
    # Truncate + load: simple, idempotent, fits the volume.
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        schema=[bigquery.SchemaField(f["name"], f["type"], mode=f["mode"]) for f in schema],
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    if not rows:
        # Truncate-to-empty: send a tiny no-op load so the table is left empty
        # and consistent with the source.
        client.query(f"DELETE FROM `{table.full_table_id.replace(':', '.')}` WHERE TRUE").result()
        return 0

    import io
    import json

    buf = io.StringIO()
    for r in rows:
        buf.write(json.dumps(r))
        buf.write("\n")
    buf.seek(0)

    job = client.load_table_from_file(
        buf, table, job_config=job_config, rewind=True
    )
    job.result()  # wait
    return len(rows)


def _ensure_dataset(client, project: str, dataset: str, location: str):
    from google.cloud import bigquery

    dataset_id = f"{project}.{dataset}"
    try:
        return client.get_dataset(dataset_id)
    except Exception:
        ds = bigquery.Dataset(dataset_id)
        ds.location = location
        return client.create_dataset(ds)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--household", default=None, help="Household name; defaults to DEFAULT_HOUSEHOLD_NAME")
    p.add_argument("--dataset", default=os.getenv("BUDGET_AGENT_BQ_DATASET", "budget_agent"))
    p.add_argument("--bq-location", default=os.getenv("BUDGET_AGENT_BQ_LOCATION", "US"),
                   help="BQ dataset storage location (US, EU, or a region). Default: US")
    p.add_argument("--dry-run", action="store_true",
                   help="Print row counts and the first row of each table; do not write to BQ.")
    args = p.parse_args()

    cfg = settings()
    project = cfg.gcp_project
    if not project:
        print("GOOGLE_CLOUD_PROJECT is not set", file=sys.stderr)
        return 2

    household = args.household or cfg.default_household_name
    synced_at = datetime.now(tz=timezone.utc)

    payload = _build_payload(household, synced_at)
    print(f"Source: Postgres household '{household}' ({payload.household_id})")
    print(f"  categories:    {len(payload.categories):>5}")
    print(f"  budgets:       {len(payload.budgets):>5}")
    print(f"  transactions:  {len(payload.transactions):>5}")

    if args.dry_run:
        print("\n--dry-run: no writes. Sample rows below.")
        for name, rows in [
            ("categories", payload.categories),
            ("budgets", payload.budgets),
            ("transactions", payload.transactions),
        ]:
            print(f"\n[{name}] {len(rows)} rows")
            if rows:
                first = rows[0]
                for k, v in first.items():
                    print(f"  {k}: {v}")
        print(f"\nTarget would have been: {project}.{args.dataset}.* in {args.bq_location}")
        return 0

    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    _ensure_dataset(client, project, args.dataset, args.bq_location)
    dataset_ref = f"{project}.{args.dataset}"

    cat_n = _sync_table(client, dataset_ref, "categories", CATEGORIES_SCHEMA, payload.categories)
    bud_n = _sync_table(client, dataset_ref, "budgets", BUDGETS_SCHEMA, payload.budgets)
    tx_n = _sync_table(client, dataset_ref, "transactions", TRANSACTIONS_SCHEMA, payload.transactions)

    print(f"\nWrote to {dataset_ref}:")
    print(f"  categories:    {cat_n:>5}")
    print(f"  budgets:       {bud_n:>5}")
    print(f"  transactions:  {tx_n:>5}")
    print(f"Synced at:       {synced_at.isoformat()}")
    print("\nQuery example:")
    print("  bq query --use_legacy_sql=false 'SELECT category_name, SUM(amount_nok) AS total")
    print(f"     FROM `{dataset_ref}.transactions` WHERE is_income IS NOT TRUE")
    print("     GROUP BY 1 ORDER BY 2 DESC'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
