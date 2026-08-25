import time

import session_sticky as mod
from session_sticky import SessionStickyStore, resolve_session_id


def test_resolve_prefers_hermes_session_header():
    body = {"user": "body-user", "metadata": {"session_id": "meta"}}
    headers = {"X-Hermes-Session-Id": "hdr", "X-Chat-ID": "chat"}
    assert resolve_session_id(headers, body) == "hdr"


def test_resolve_falls_back_chat_id_then_user_then_metadata():
    assert resolve_session_id({"X-Chat-ID": "c"}, {"user": "u"}) == "c"
    assert resolve_session_id({}, {"user": "u", "metadata": {"sessionId": "m"}}) == "u"
    assert resolve_session_id({}, {"metadata": {"session_id": "a"}}) == "a"
    assert resolve_session_id({}, {"metadata": {"sessionId": "b"}}) == "b"
    assert resolve_session_id({}, {}) is None
    assert resolve_session_id({}, None) is None


def test_sticky_set_get_clear():
    s = SessionStickyStore(ttl_s=3600, max_entries=100)
    assert s.get("s1") is None
    s.set("s1", provider="groq", model="llama", key="k1")
    got = s.get("s1")
    assert got["provider"] == "groq" and got["model"] == "llama" and got["key"] == "k1"
    s.clear("s1")
    assert s.get("s1") is None


def test_sticky_ttl_expires(monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000.0)
    s = SessionStickyStore(ttl_s=10, max_entries=100)
    with s._lock:
        s._entries["s1"] = {
            "provider": "a",
            "model": "m",
            "key": "k",
            "updated_at": 1_000_000.0 - 11,
        }
    assert s.get("s1") is None


def test_sticky_ttl_zero_never_idle_expires(monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000.0)
    s = SessionStickyStore(ttl_s=0, max_entries=100)
    with s._lock:
        s._entries["s1"] = {
            "provider": "a",
            "model": "m",
            "key": "k",
            "updated_at": 1_000_000.0 - 86_400,
        }
    got = s.get("s1")
    assert got is not None and got["key"] == "k"
    s.clear("s1")
    assert s.get("s1") is None


def test_sticky_default_ttl_is_300():
    assert SessionStickyStore().ttl_s == 300.0


def test_sticky_hard_cap_evicts_oldest():
    s = SessionStickyStore(ttl_s=3600, max_entries=2)
    s.set("a", provider="p", model="m", key="1")
    time.sleep(0.01)
    s.set("b", provider="p", model="m", key="2")
    time.sleep(0.01)
    s.set("c", provider="p", model="m", key="3")
    assert s.get("a") is None
    assert s.get("b") is not None and s.get("c") is not None
