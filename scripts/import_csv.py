"""CLI to import a CSV into a household.

Usage:
  python -m scripts.import_csv data/sample/sample_import.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings
from app.db.repository import get_household_by_name
from app.db.session import session_scope
from app.tools.csv_import import import_csv


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("path", type=Path)
    p.add_argument("--household", default=None, help="Household name (defaults to env)")
    args = p.parse_args()

    name = args.household or settings().default_household_name
    with session_scope() as s:
        h = get_household_by_name(s, name)
        if h is None:
            print(f"household '{name}' not found", file=sys.stderr)
            return 1
        with args.path.open() as f:
            result = import_csv(s, h.id, f)

    print(f"Inserted: {result.inserted}")
    if result.rejected:
        print(f"Rejected: {len(result.rejected)}")
        for i, reason, raw in result.rejected:
            print(f"  row {i}: {reason} :: {raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
