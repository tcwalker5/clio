"""
preview_store.py — Ephemeral in-memory handoff between a dry-run preview and
the "confirm and post live" step, keyed by a one-time token.

Not persisted to SQLite: this is a few seconds of dashboard state for one
staff member's upload, not data worth surviving a restart. A dict is enough
at this scale (a handful of LAN users, one preview at a time each).
"""

PREVIEWS: dict[str, dict] = {}
