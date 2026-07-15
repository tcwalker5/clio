"""
matters_csv.py — Shared reader for data/clio-matters.csv, the matters export
that carries fields the Clio API doesn't expose directly: Responsible/
Originating Attorney, Responsible Staff (client_list.py), and Court Case
Number (matcher.py's case-number cross-check). Refresh the CSV per
CLAUDE.md's documented workflow to keep these current.
"""

import csv
from pathlib import Path

MATTERS_CSV = Path("data/clio-matters.csv")


def load_open_matter_rows() -> list[dict]:
    """Raw CSV rows, open matters only."""
    if not MATTERS_CSV.exists():
        raise FileNotFoundError(f"Matters export not found: {MATTERS_CSV}")

    with open(MATTERS_CSV, encoding="utf-8-sig") as f:
        return [row for row in csv.DictReader(f) if (row.get("Status") or "").strip().lower() == "open"]


def index_case_numbers_by_matter_id(rows: list[dict]) -> dict[int, str]:
    """Clio matter ID -> on-file Court Case Number (empty string if not set)."""
    result: dict[int, str] = {}
    for row in rows:
        uid = (row.get("Unique ID") or "").strip()
        if not uid:
            continue
        result[int(uid)] = (row.get("Court Case Number") or "").strip().upper()
    return result
