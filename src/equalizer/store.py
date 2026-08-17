"""
store.py — SQLite persistence for Equalizer worksheets/items
(data/clio_dashboard.db). No Clio calls here — matter linkage is just an id +
display_number snapshot; matter_id is the only tie back to the live system,
resolved fresh wherever the matter's own name/status is needed.

SCHEMA/SCHEMA_COLUMNS below are this module's own schema fragment — owned
here rather than in web/db.py's schema, so a mistake in Equalizer's tables
can't break every other subproject's tables too. web/db.py's get_connection()
applies every subproject's fragment in its own isolated try/except; see that
file's _apply_fragment() for the mechanism (moved 2026-08-17, alongside
building the Moore/Marsden Calculator, once a second subproject made "one
shared SCHEMA string, one executescript() call" a real blast-radius risk
rather than a hypothetical one).
"""

# One worksheet per asset/debt division exercise, tied to a Clio matter.
# naming_mode 'husband_wife' (default) shows fixed H/W labels; 'first_names'
# (the same-sex-couple case) shows party_a_label/party_b_label instead,
# editable and normally pre-filled from the matter's client + Opposing Party
# contact (see equalizer/clio_parties.py). Tax rates are per-worksheet, one
# set of four (fed/state/long-term/short-term capital gain) per party — a
# row picks which one applies via equalizer_items.rate_type, it does not
# carry its own rate.
#
# status: 'draft' (never pushed to Clio) or 'saved' (pushed at least once)
# — purely informational, does NOT lock editing (Ted, 2026-08-14: "nothing
# is ever final" in this line of work — a worksheet stays editable after
# being saved to Clio, and Save to Clio can be clicked again any time,
# pushing a new Document *version* rather than a duplicate file — see
# equalizer/clio_documents.py). finalized_at is actually "first saved to
# Clio at" (column name kept as-is, no real data existed to migrate when
# the terminology changed); last_saved_at updates on every save, including
# the first.
SCHEMA = """
CREATE TABLE IF NOT EXISTS equalizer_worksheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matter_id INTEGER NOT NULL,
    matter_display_number TEXT NOT NULL,
    naming_mode TEXT NOT NULL DEFAULT 'husband_wife',
    party_a_label TEXT NOT NULL DEFAULT 'Husband',
    party_b_label TEXT NOT NULL DEFAULT 'Wife',
    party_a_role TEXT NOT NULL DEFAULT 'Petitioner',
    party_b_role TEXT NOT NULL DEFAULT 'Respondent',
    -- Defaults match the legacy Propertizer tool's own (see DEFAULT_* constants
    -- below, which are what actually apply these — a column DEFAULT here only
    -- governs a database created fresh with this schema, not one where the
    -- column already existed).
    fed_rate_a REAL NOT NULL DEFAULT 0.25,
    fed_rate_b REAL NOT NULL DEFAULT 0.25,
    state_rate_a REAL NOT NULL DEFAULT 0.093,
    state_rate_b REAL NOT NULL DEFAULT 0.093,
    lt_rate_a REAL NOT NULL DEFAULT 0.15,
    lt_rate_b REAL NOT NULL DEFAULT 0.15,
    st_rate_a REAL NOT NULL DEFAULT 0.25,
    st_rate_b REAL NOT NULL DEFAULT 0.25,
    status TEXT NOT NULL DEFAULT 'draft',
    clio_document_id INTEGER,
    -- Staff-chosen filename from the last successful Save to Clio (no
    -- extension needed to type it, .pdf gets appended) — lets multiple
    -- scenario worksheets for the same matter/day stay distinguishable in
    -- Clio (matches the legacy Propertizer workflow of naming each
    -- scenario run) instead of colliding on the same date-stamped name,
    -- and doubles as the recall list's only way to tell worksheets apart
    -- once more than one exists for a matter (see equalizer_matter.html).
    clio_document_name TEXT,
    finalized_at TEXT,
    last_saved_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_equalizer_worksheets_matter ON equalizer_worksheets(matter_id);

-- Equity (fmv - debt) is deliberately not stored — computed at read time so
-- it can never drift from its inputs (same reasoning as trust_monitor's
-- cushion/shortfall properties). after_tax_a/b are nullable: NULL means
-- "auto-computed from before_tax +/- the unrealized-gain tax hit" (see
-- equalizer/calc.py); a non-null value is a manual override a paralegal
-- typed in directly, which always wins.
CREATE TABLE IF NOT EXISTS equalizer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    worksheet_id INTEGER NOT NULL REFERENCES equalizer_worksheets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    fmv REAL NOT NULL DEFAULT 0,
    debt REAL NOT NULL DEFAULT 0,
    before_tax_a REAL NOT NULL DEFAULT 0,
    before_tax_b REAL NOT NULL DEFAULT 0,
    tax_basis REAL,
    rate_type TEXT NOT NULL DEFAULT 'none',
    gain_loss INTEGER NOT NULL DEFAULT 0,
    after_tax_a REAL,
    after_tax_b REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_equalizer_items_worksheet ON equalizer_items(worksheet_id, position);
"""

# (table, column, ddl) — applied via web/db.py's generic _ensure_column() for
# databases that predate a given column (schema drift guard, not a real
# migration framework; see that function's own docstring).
SCHEMA_COLUMNS = [
    ("equalizer_worksheets", "last_saved_at", "TEXT"),
    ("equalizer_worksheets", "clio_document_name", "TEXT"),
]

# Starting tax rates for every new worksheet — matches the legacy
# Propertizer tool's own defaults exactly (confirmed 2026-08-14 against a
# real Propertizer PDF output: Federal 25%, State 9.3%, LT gain 15%, ST
# gain 25%, same for both parties). Explicit here rather than relying on
# the DB column DEFAULT alone — that only takes effect for a database
# created fresh with today's schema, not one where these columns already
# exist from before this change (SQLite doesn't retroactively change a
# column's DEFAULT when CREATE TABLE IF NOT EXISTS's text changes).
DEFAULT_FED_RATE = 0.25
DEFAULT_STATE_RATE = 0.093
DEFAULT_LT_RATE = 0.15
DEFAULT_ST_RATE = 0.25


def create_worksheet(
    conn, matter_id: int, matter_display_number: str,
    party_a_role: str = "Petitioner", party_b_role: str = "Respondent",
) -> int:
    cur = conn.execute(
        """INSERT INTO equalizer_worksheets (
               matter_id, matter_display_number, party_a_role, party_b_role,
               fed_rate_a, fed_rate_b, state_rate_a, state_rate_b,
               lt_rate_a, lt_rate_b, st_rate_a, st_rate_b
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            matter_id, matter_display_number, party_a_role, party_b_role,
            DEFAULT_FED_RATE, DEFAULT_FED_RATE, DEFAULT_STATE_RATE, DEFAULT_STATE_RATE,
            DEFAULT_LT_RATE, DEFAULT_LT_RATE, DEFAULT_ST_RATE, DEFAULT_ST_RATE,
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_worksheets(conn, status: str | None = None) -> list[dict]:
    """status=None returns every worksheet; pass "draft" to get only
    in-progress ones — the landing page's own browsable list uses that
    filter, since a flat list of every worksheet ever saved to Clio across
    hundreds of clients isn't something anyone wants to scroll (recall a
    saved one via the matter search instead, see list_worksheets_by_matter)."""
    if status is None:
        rows = conn.execute("SELECT * FROM equalizer_worksheets ORDER BY updated_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM equalizer_worksheets WHERE status = ? ORDER BY updated_at DESC", (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_worksheets_by_matter(conn, matter_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM equalizer_worksheets WHERE matter_id = ? ORDER BY created_at DESC", (matter_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_worksheet(conn, worksheet_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM equalizer_worksheets WHERE id = ?", (worksheet_id,)).fetchone()
    return dict(row) if row else None


def list_items(conn, worksheet_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM equalizer_items WHERE worksheet_id = ? ORDER BY position", (worksheet_id,)
    ).fetchall()
    return [dict(r) for r in rows]


_SETTINGS_FIELDS = (
    "naming_mode", "party_a_label", "party_b_label", "party_a_role", "party_b_role",
    "fed_rate_a", "fed_rate_b", "state_rate_a", "state_rate_b",
    "lt_rate_a", "lt_rate_b", "st_rate_a", "st_rate_b",
)


def update_worksheet_settings(conn, worksheet_id: int, **fields) -> None:
    unknown = set(fields) - set(_SETTINGS_FIELDS)
    if unknown:
        raise ValueError(f"Unknown worksheet settings field(s): {unknown}")
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE equalizer_worksheets SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), worksheet_id),
    )
    conn.commit()


def mark_saved_to_clio(conn, worksheet_id: int, clio_document_id: int, clio_document_name: str) -> None:
    """Called every time Save to Clio succeeds, not just the first — a
    worksheet stays editable and re-savable indefinitely (Ted, 2026-08-14).
    finalized_at only gets set the first time (COALESCE — it's "first saved
    at", the column name is just a holdover); last_saved_at and
    clio_document_name always update, so a later save with a different
    chosen filename overwrites the recall list's label too."""
    conn.execute(
        """UPDATE equalizer_worksheets
           SET status = 'saved', clio_document_id = ?, clio_document_name = ?,
               finalized_at = COALESCE(finalized_at, CURRENT_TIMESTAMP),
               last_saved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (clio_document_id, clio_document_name, worksheet_id),
    )
    conn.commit()


def duplicate_worksheet(conn, worksheet_id: int) -> int | None:
    """"Save As" — clones a worksheet's settings and every item into a
    brand-new draft, for running a variant scenario off an existing one
    (Ted, 2026-08-14: staff may run several scenarios for the same matter
    in one day) without retyping everything. The copy starts completely
    fresh on the Clio side (status 'draft', no clio_document_id/name) — an
    existing Save-to-Clio link belongs to the original worksheet, not the
    copy; the copy gets its own the first time it's saved. Returns None if
    the source worksheet doesn't exist."""
    source = get_worksheet(conn, worksheet_id)
    if source is None:
        return None

    cur = conn.execute(
        """INSERT INTO equalizer_worksheets (
               matter_id, matter_display_number, naming_mode,
               party_a_label, party_b_label, party_a_role, party_b_role,
               fed_rate_a, fed_rate_b, state_rate_a, state_rate_b,
               lt_rate_a, lt_rate_b, st_rate_a, st_rate_b
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source["matter_id"], source["matter_display_number"], source["naming_mode"],
            source["party_a_label"], source["party_b_label"], source["party_a_role"], source["party_b_role"],
            source["fed_rate_a"], source["fed_rate_b"], source["state_rate_a"], source["state_rate_b"],
            source["lt_rate_a"], source["lt_rate_b"], source["st_rate_a"], source["st_rate_b"],
        ),
    )
    new_worksheet_id = cur.lastrowid

    for item in list_items(conn, worksheet_id):
        conn.execute(
            """INSERT INTO equalizer_items (
                   worksheet_id, position, description, fmv, debt,
                   before_tax_a, before_tax_b, tax_basis, rate_type, gain_loss,
                   after_tax_a, after_tax_b
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_worksheet_id, item["position"], item["description"], item["fmv"], item["debt"],
                item["before_tax_a"], item["before_tax_b"], item["tax_basis"], item["rate_type"], item["gain_loss"],
                item["after_tax_a"], item["after_tax_b"],
            ),
        )

    conn.commit()
    return new_worksheet_id


def delete_worksheet(conn, worksheet_id: int) -> None:
    """Items cascade via the equalizer_items.worksheet_id foreign key
    (ON DELETE CASCADE, web/db.py). Callers are responsible for only
    offering this on worksheets never saved to Clio (clio_document_id is
    NULL) — one that has already has a real PDF and a matter Note pointing
    at it in Clio, so deleting the local record would orphan both rather
    than actually cleaning anything up."""
    conn.execute("DELETE FROM equalizer_worksheets WHERE id = ?", (worksheet_id,))
    conn.commit()


def add_item(conn, worksheet_id: int, description: str = "") -> int:
    next_position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM equalizer_items WHERE worksheet_id = ?", (worksheet_id,)
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO equalizer_items (worksheet_id, position, description) VALUES (?, ?, ?)",
        (worksheet_id, next_position, description),
    )
    conn.execute("UPDATE equalizer_worksheets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (worksheet_id,))
    conn.commit()
    return cur.lastrowid


_ITEM_FIELDS = (
    "description", "fmv", "debt", "before_tax_a", "before_tax_b",
    "tax_basis", "rate_type", "gain_loss", "after_tax_a", "after_tax_b",
)


def update_item(conn, item_id: int, **fields) -> None:
    unknown = set(fields) - set(_ITEM_FIELDS)
    if unknown:
        raise ValueError(f"Unknown item field(s): {unknown}")
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE equalizer_items SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (*fields.values(), item_id),
    )
    row = conn.execute("SELECT worksheet_id FROM equalizer_items WHERE id = ?", (item_id,)).fetchone()
    if row:
        conn.execute("UPDATE equalizer_worksheets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row[0],))
    conn.commit()


def delete_item(conn, item_id: int) -> None:
    row = conn.execute("SELECT worksheet_id FROM equalizer_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM equalizer_items WHERE id = ?", (item_id,))
    conn.execute("UPDATE equalizer_worksheets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row[0],))
    conn.commit()
