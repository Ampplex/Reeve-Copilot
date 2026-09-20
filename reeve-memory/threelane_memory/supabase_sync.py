"""Server-side mirror of the authenticated user's profile into Supabase.

The frontend used to write user rows (email, name, avatar) directly with the
anon key. With RLS enabled that write is blocked, so the backend now performs
it with the service-role key. Everything here is best-effort: a Supabase
outage or misconfiguration must never break authentication.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from threelane_memory.config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

_client: Any | None = None


def is_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _get_client() -> Any | None:
    """Return a cached service-role Supabase client, or None if unconfigured."""
    global _client
    if not is_configured():
        return None
    if _client is None:
        # Imported lazily so the dependency is only needed when Supabase is
        # actually configured (e.g. the network server), not for CLI/stdio use.
        from supabase import create_client

        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client


def sync_user_profile(
    user_id: str,
    *,
    email: str | None = None,
    name: str | None = None,
    picture: str | None = None,
) -> bool:
    """Upsert the user's profile into the Supabase ``users`` table.

    Keyed on ``google_uid``. Returns True on a successful write, False if
    Supabase is unconfigured or the write failed (never raises).
    """
    client = _get_client()
    if client is None:
        return False

    row: dict[str, Any] = {
        "google_uid": user_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Only send claims we actually have so we never overwrite existing columns
    # with nulls when a token happens to omit a field.
    if email is not None:
        row["email"] = email
    if name is not None:
        row["name"] = name
    if picture is not None:
        row["avatar_url"] = picture

    try:
        client.table("users").upsert(row, on_conflict="google_uid").execute()
        return True
    except Exception as exc:
        logger.warning("Supabase user upsert failed for uid=%s: %s", user_id, exc)
        return False
