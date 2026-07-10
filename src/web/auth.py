"""
auth.py — Shared-passphrase login for the dashboard.

Not per-user auth (the Clio API side already uses one shared app registration
per CLAUDE.md). This just keeps the LAN-accessible dashboard from being wide
open — a single passphrase gates a signed session cookie.
"""

import os

from fastapi import Request

PASSPHRASE = os.getenv("CLIO_DASHBOARD_PASSPHRASE", "")


class AuthRequired(Exception):
    """Raised by require_auth; handled by app.py to redirect to /login."""


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def require_auth(request: Request) -> None:
    """FastAPI dependency — raises AuthRequired if not logged in."""
    if not is_authenticated(request):
        raise AuthRequired()
