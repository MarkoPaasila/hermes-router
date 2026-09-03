"""``x-opencode-session`` — OpenCode backend-affinity header.

OpenCode (opencode.ai Zen/Go) pins requests that share an
``x-opencode-session`` value to the same upstream backend so its prompt
cache stays warm. Starting 2026-09-06, requests without the header may
error.

The value is the client session id when one was supplied; otherwise a
request-scoped opaque UUID. That UUID is only for this upstream header —
it is not a Session and is not stored in session affinity.
"""
from __future__ import annotations

import threading
import uuid
from urllib.parse import urlparse

OPENCODE_SESSION_HEADER = "x-opencode-session"

_ctx = threading.local()


def reset_request_session() -> None:
    """Clear request-scoped session state (tests and new inbound requests)."""
    _ctx.session_id = None
    _ctx.opencode_session = None


def seed_request_session(session_id: str | None = None) -> None:
    """Bind this inbound request's client session id; reset any synthetic key."""
    sid = str(session_id).strip() if session_id else None
    _ctx.session_id = sid or None
    _ctx.opencode_session = None


def is_opencode_target(name: str | None, base_url: str | None) -> bool:
    """True when *name* or *base_url* addresses the OpenCode relay."""
    n = (name or "").strip().lower()
    if n in ("opencode", "opencode_go"):
        return True
    host = urlparse(str(base_url or "")).hostname or ""
    host = host.lower().rstrip(".")
    return host == "opencode.ai" or host.endswith(".opencode.ai")


def opencode_session_value(session_id: str | None = None) -> str:
    """Client session id, else a request-scoped UUID, else a fresh UUID."""
    if session_id and str(session_id).strip():
        return str(session_id).strip()
    sid = getattr(_ctx, "session_id", None)
    if sid:
        return sid
    syn = getattr(_ctx, "opencode_session", None)
    if syn:
        return syn
    syn = str(uuid.uuid4())
    _ctx.opencode_session = syn
    return syn


def merge_opencode_session_headers(
    headers: dict,
    provider: dict,
    session_id: str | None = None,
) -> dict:
    """Set ``x-opencode-session`` on OpenCode targets. Existing keys win.

    Mutates *headers* and returns it.
    """
    if not is_opencode_target(provider.get("name"), provider.get("base_url")):
        return headers
    headers.setdefault(OPENCODE_SESSION_HEADER, opencode_session_value(session_id))
    return headers
