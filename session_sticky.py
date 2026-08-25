"""Session-sticky routing state for hermes-router."""
from __future__ import annotations

import threading
import time
from typing import Any


def resolve_session_id(headers: dict, body: dict | None) -> str | None:
    """First non-empty session id from headers/body, else None."""
    def _h(*names: str) -> str | None:
        for n in names:
            for k, v in (headers or {}).items():
                if k.lower() == n.lower() and str(v).strip():
                    return str(v).strip()
        return None

    sid = _h("X-Hermes-Session-Id")
    if sid:
        return sid
    sid = _h("X-Chat-ID")
    if sid:
        return sid
    body = body or {}
    user = body.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    for key in ("session_id", "sessionId"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


class SessionStickyStore:
    def __init__(self, ttl_s: float = 300.0, max_entries: int = 10_000):
        self.ttl_s = float(ttl_s)
        self.max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, session_id: str) -> dict | None:
        if not session_id:
            return None
        with self._lock:
            e = self._entries.get(session_id)
            if not e:
                return None
            # ttl_s <= 0: no idle expiry (cascade/clear still drop entries).
            if self.ttl_s > 0 and time.time() - e["updated_at"] > self.ttl_s:
                del self._entries[session_id]
                return None
            return dict(e)

    def set(self, session_id: str, *, provider: str, model: str, key: str) -> None:
        if not session_id:
            return
        now = time.time()
        with self._lock:
            self._entries[session_id] = {
                "provider": provider,
                "model": model,
                "key": key,
                "updated_at": now,
            }
            self._evict_unlocked()

    def clear(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._entries.pop(session_id, None)

    def _evict_unlocked(self) -> None:
        now = time.time()
        if self.ttl_s > 0:
            expired = [k for k, e in self._entries.items()
                       if now - e["updated_at"] > self.ttl_s]
            for k in expired:
                del self._entries[k]
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.items(), key=lambda kv: kv[1]["updated_at"])[0]
            del self._entries[oldest]
