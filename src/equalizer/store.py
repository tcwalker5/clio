"""
store.py — SQLite persistence for Equalizer worksheets/items
(data/clio_dashboard.db, schema in web/db.py). No Clio calls here — matter
linkage is just an id + display_number snapshot; matter_id is the only tie
back to the live system, resolved fresh wherever the matter's own name/status
is needed.
"""


def create_worksheet(
    conn, matter_id: int, matter_display_number: str,
    party_a_role: str = "Petitioner", party_b_role: str = "Respondent",
) -> int:
    cur = conn.execute(
        """INSERT INTO equalizer_worksheets (matter_id, matter_display_number, party_a_role, party_b_role)
           VALUES (?, ?, ?, ?)""",
        (matter_id, matter_display_number, party_a_role, party_b_role),
    )
    conn.commit()
    return cur.lastrowid


def list_worksheets(conn, status: str | None = None) -> list[dict]:
    """status=None returns every worksheet; pass "draft" to get only
    in-progress ones — the landing page's own browsable list uses that
    filter, since a flat list of every worksheet ever finalized across
    hundreds of clients isn't something anyone wants to scroll (recall a
    finalized one via the matter search instead, see list_worksheets_by_matter)."""
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


def finalize_worksheet(conn, worksheet_id: int, clio_document_id: int) -> None:
    conn.execute(
        """UPDATE equalizer_worksheets
           SET status = 'finalized', clio_document_id = ?, finalized_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (clio_document_id, worksheet_id),
    )
    conn.commit()


def delete_worksheet(conn, worksheet_id: int) -> None:
    """Items cascade via the equalizer_items.worksheet_id foreign key
    (ON DELETE CASCADE, web/db.py). Callers are responsible for only
    offering this on draft worksheets — a finalized one already has a PDF
    and a matter Note pointing at it in Clio, so deleting the local record
    would orphan both rather than actually cleaning anything up."""
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
