"""
store.py — SQLite persistence for Moore/Marsden worksheets/segments
(data/clio_dashboard.db). No Clio calls here — matter linkage is just an id +
display_number snapshot; matter_id is the only tie back to the live system,
resolved fresh wherever the matter's own name/status is needed.

SCHEMA/SCHEMA_COLUMNS below are this module's own schema fragment, applied
by web/db.py's get_connection() in its own isolated try/except (see that
file's _apply_fragment()) — a mistake in these tables can't break any other
subproject's tables. Same pattern as equalizer/store.py.
"""

# One worksheet per Moore/Marsden calculation (California community-property
# interest in a spouse's separate-property real estate), tied to a Clio
# matter. Mirrors equalizer_worksheets' status/Clio-linkage shape (draft/
# saved, repeatable Save to Clio, clio_document_name for the recall list).
#
# acquired_before_marriage + value_at_date_of_marriage only matter for the
# classic pre-marital-purchase fact pattern: community percentage is always
# anchored to the original purchase price (the 'purchase' row in
# moore_marsden_segments), but marital appreciation is measured from
# value_at_date_of_marriage instead of the purchase price when the home
# predates the marriage, so premarital appreciation stays separate property.
SCHEMA = """
CREATE TABLE IF NOT EXISTS moore_marsden_worksheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id INTEGER NOT NULL,
    matter_display_number TEXT NOT NULL,
    owner_spouse_label TEXT NOT NULL DEFAULT '',
    non_owner_spouse_label TEXT NOT NULL DEFAULT '',
    acquired_before_marriage INTEGER NOT NULL DEFAULT 0,
    date_of_marriage TEXT,
    value_at_date_of_marriage REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    clio_document_id INTEGER,
    clio_document_name TEXT,
    finalized_at TEXT,
    last_saved_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_moore_marsden_worksheets_matter ON moore_marsden_worksheets(matter_id);

-- Ordered rows chaining the calculation: exactly one 'purchase' at position 0,
-- exactly one 'valuation' at the last position, zero or more 'refinance' rows
-- between them. property_value is the value established at this event
-- (purchase price / refi appraisal / current valuation) — it doubles as the
-- *end* of this row's own period and the *basis* the next row's community
-- percentage is computed against (see moore_marsden/calc.py). loan_balance is
-- a memo field only (shown on the PDF's date/description/amount log, not
-- used in the math). community_principal_reduction is THIS PERIOD's own
-- community-funds-sourced paydown (staff-typed from mortgage statements,
-- 0 on the purchase row, which has no prior period) — calc.py carries the
-- running cumulative total forward itself, so staff never have to hand-sum
-- it the way the legacy Excel tool's preparer did.
CREATE TABLE IF NOT EXISTS moore_marsden_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worksheet_id INTEGER NOT NULL REFERENCES moore_marsden_worksheets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    segment_type TEXT NOT NULL,
    event_date TEXT,
    event_label TEXT,
    property_value REAL NOT NULL DEFAULT 0,
    loan_balance REAL,
    community_principal_reduction REAL NOT NULL DEFAULT 0,
    sp_contribution REAL NOT NULL DEFAULT 0,
    cp_contribution REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_moore_marsden_segments_worksheet ON moore_marsden_segments(worksheet_id, position);
"""

# (table, column, ddl) — applied via web/db.py's generic _ensure_column() for
# databases that predate a given column. Empty for now; grows the same way
# equalizer/store.py's did, as real schema changes land after this ships.
SCHEMA_COLUMNS: list[tuple[str, str, str]] = []


def create_worksheet(
    conn, matter_id: int, matter_display_number: str,
    owner_spouse_label: str = "", non_owner_spouse_label: str = "",
) -> int:
    """Also seeds the two structurally-required rows every worksheet must
    have — a 'purchase' row at position 0 and a 'valuation' row at position 1
    — since the calculation is undefined without both (see calc.py). Unlike
    Equalizer's items, these aren't optional starter content; add_segment()
    only ever inserts 'refinance' rows between them."""
    cur = conn.execute(
        """INSERT INTO moore_marsden_worksheets (
               matter_id, matter_display_number, owner_spouse_label, non_owner_spouse_label
           ) VALUES (?, ?, ?, ?)""",
        (matter_id, matter_display_number, owner_spouse_label, non_owner_spouse_label),
    )
    worksheet_id = cur.lastrowid
    conn.execute(
        "INSERT INTO moore_marsden_segments (worksheet_id, position, segment_type) VALUES (?, 0, 'purchase')",
        (worksheet_id,),
    )
    conn.execute(
        "INSERT INTO moore_marsden_segments (worksheet_id, position, segment_type) VALUES (?, 1, 'valuation')",
        (worksheet_id,),
    )
    conn.commit()
    return worksheet_id


def list_worksheets(conn, status: str | None = None) -> list[dict]:
    """status=None returns every worksheet; pass "draft" to get only
    in-progress ones — same reasoning as equalizer.store.list_worksheets:
    the landing page's browsable list only shows drafts, recall a saved one
    via matter search instead (list_worksheets_by_matter)."""
    if status is None:
        rows = conn.execute("SELECT * FROM moore_marsden_worksheets ORDER BY updated_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM moore_marsden_worksheets WHERE status = ? ORDER BY updated_at DESC", (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_worksheets_by_matter(conn, matter_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM moore_marsden_worksheets WHERE matter_id = ? ORDER BY created_at DESC", (matter_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_worksheet(conn, worksheet_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM moore_marsden_worksheets WHERE id = ?", (worksheet_id,)).fetchone()
    return dict(row) if row else None


def list_segments(conn, worksheet_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM moore_marsden_segments WHERE worksheet_id = ? ORDER BY position", (worksheet_id,)
    ).fetchall()
    return [dict(r) for r in rows]


_SETTINGS_FIELDS = (
    "owner_spouse_label", "non_owner_spouse_label",
    "acquired_before_marriage", "date_of_marriage", "value_at_date_of_marriage",
)


def update_worksheet_settings(conn, worksheet_id: int, **fields) -> None:
    unknown = set(fields) - set(_SETTINGS_FIELDS)
    if unknown:
        raise ValueError(f"Unknown worksheet settings field(s): {unknown}")
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE moore_marsden_worksheets SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), worksheet_id),
    )
    conn.commit()


def mark_saved_to_clio(conn, worksheet_id: int, clio_document_id: int, clio_document_name: str) -> None:
    """Called every time Save to Clio succeeds, not just the first — same
    repeatable-save behavior as Equalizer (see equalizer/store.py's own
    mark_saved_to_clio for the reasoning)."""
    conn.execute(
        """UPDATE moore_marsden_worksheets
           SET status = 'saved', clio_document_id = ?, clio_document_name = ?,
               finalized_at = COALESCE(finalized_at, CURRENT_TIMESTAMP),
               last_saved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (clio_document_id, clio_document_name, worksheet_id),
    )
    conn.commit()


def duplicate_worksheet(conn, worksheet_id: int) -> int | None:
    """"Save As" — clones a worksheet's settings and every segment (purchase,
    every refinance, and valuation) into a brand-new draft, for running a
    variant scenario without retyping the whole chain. The copy starts fresh
    on the Clio side (status 'draft', no clio_document_id/name) — same
    reasoning as equalizer.store.duplicate_worksheet. Returns None if the
    source worksheet doesn't exist."""
    source = get_worksheet(conn, worksheet_id)
    if source is None:
        return None

    cur = conn.execute(
        """INSERT INTO moore_marsden_worksheets (
               matter_id, matter_display_number, owner_spouse_label, non_owner_spouse_label,
               acquired_before_marriage, date_of_marriage, value_at_date_of_marriage
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            source["matter_id"], source["matter_display_number"],
            source["owner_spouse_label"], source["non_owner_spouse_label"],
            source["acquired_before_marriage"], source["date_of_marriage"], source["value_at_date_of_marriage"],
        ),
    )
    new_worksheet_id = cur.lastrowid

    for seg in list_segments(conn, worksheet_id):
        conn.execute(
            """INSERT INTO moore_marsden_segments (
                   worksheet_id, position, segment_type, event_date, event_label, property_value,
                   loan_balance, community_principal_reduction, sp_contribution, cp_contribution, notes
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_worksheet_id, seg["position"], seg["segment_type"], seg["event_date"], seg["event_label"],
                seg["property_value"], seg["loan_balance"], seg["community_principal_reduction"],
                seg["sp_contribution"], seg["cp_contribution"], seg["notes"],
            ),
        )

    conn.commit()
    return new_worksheet_id


def delete_worksheet(conn, worksheet_id: int) -> None:
    """Segments cascade via moore_marsden_segments.worksheet_id's foreign key
    (ON DELETE CASCADE). Callers are responsible for only offering this on
    worksheets never saved to Clio (clio_document_id is NULL) — same rule as
    Equalizer, for the same reason: a saved worksheet already has a real
    Document + matter Note pointing at it in Clio."""
    conn.execute("DELETE FROM moore_marsden_worksheets WHERE id = ?", (worksheet_id,))
    conn.commit()


def add_segment(conn, worksheet_id: int) -> int:
    """Inserts a new 'refinance' row immediately before the worksheet's
    (single, required) 'valuation' row, shifting the valuation row's
    position by one to make room — refinances always land between the fixed
    purchase and valuation endpoints. Raises if the worksheet has no
    valuation row, which should never happen outside a bug (create_worksheet
    always seeds one)."""
    valuation = conn.execute(
        "SELECT id, position FROM moore_marsden_segments WHERE worksheet_id = ? AND segment_type = 'valuation'",
        (worksheet_id,),
    ).fetchone()
    if valuation is None:
        raise ValueError(f"Worksheet {worksheet_id} has no valuation row to insert a refinance before")

    new_position = valuation["position"]
    conn.execute("UPDATE moore_marsden_segments SET position = position + 1 WHERE id = ?", (valuation["id"],))
    cur = conn.execute(
        "INSERT INTO moore_marsden_segments (worksheet_id, position, segment_type) VALUES (?, ?, 'refinance')",
        (worksheet_id, new_position),
    )
    conn.execute("UPDATE moore_marsden_worksheets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (worksheet_id,))
    conn.commit()
    return cur.lastrowid


_SEGMENT_FIELDS = (
    "event_date", "event_label", "property_value", "loan_balance",
    "community_principal_reduction", "sp_contribution", "cp_contribution", "notes",
)


def update_segment(conn, segment_id: int, **fields) -> None:
    """segment_type is deliberately not editable here — it's structural
    (set once at creation via add_segment/create_worksheet, never
    reassigned) rather than a field staff type into."""
    unknown = set(fields) - set(_SEGMENT_FIELDS)
    if unknown:
        raise ValueError(f"Unknown segment field(s): {unknown}")
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE moore_marsden_segments SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), segment_id),
    )
    row = conn.execute("SELECT worksheet_id FROM moore_marsden_segments WHERE id = ?", (segment_id,)).fetchone()
    if row:
        conn.execute("UPDATE moore_marsden_worksheets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row[0],))
    conn.commit()


def delete_segment(conn, segment_id: int) -> None:
    """Only 'refinance' rows can be deleted — the purchase and valuation
    rows are structurally required (see create_worksheet). Positions are
    left with gaps after a delete rather than renumbered; ORDER BY position
    still yields correct order with gaps, so there's nothing to fix up."""
    row = conn.execute(
        "SELECT worksheet_id, segment_type FROM moore_marsden_segments WHERE id = ?", (segment_id,)
    ).fetchone()
    if row is None:
        return
    if row["segment_type"] != "refinance":
        raise ValueError("Only refinance rows can be deleted — the purchase and valuation rows are required.")
    conn.execute("DELETE FROM moore_marsden_segments WHERE id = ?", (segment_id,))
    conn.execute("UPDATE moore_marsden_worksheets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row["worksheet_id"],))
    conn.commit()
