# Session-Sticky Catalog Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace provider round-robin with full-catalog session-sticky routing (stick until cascade-away), retire default-model/rotation leftovers, and move TBF UI onto Providers (provider-wide) and Models (model scope).

**Architecture:** New `session_sticky.py` owns `SessionStickyStore` + `_resolve_session_id`. `CredentialPool` drops `ROTATION_MODE` and supports preferred-key + read-only peek. `_get_smart_ordered` stops rotating providers; chat send loop reads/writes sticky on success and clears on cascade-away from a model. Dashboard embeds scoped TBF tables; Rate limits page and default-model Reset go away.

**Tech Stack:** Python 3, Flask `router.py`, pytest, embedded dashboard JS in `router.py`, markdown docs under `documentation/` + `website/src/content/docs/`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-30-session-sticky-catalog-routing-design.md`
- Do **not** invent session ids; missing id → no stickiness
- Session id priority: `X-Hermes-Session-Id` → `X-Chat-ID` → body `user` → `metadata.session_id` / `metadata.sessionId`
- Sticky break only on cascade-away from `(provider, model)` — **not** thin headroom alone
- Sticky store: idle TTL **3600s**, hard cap **10_000**, in-memory only
- TBF ledger semantics unchanged (no rate_limiter learning changes unless required for peek/wiring)
- No Hermes Agent patch; embeddings stay non-sticky
- Class name in code is `CredentialPool` (not KeyPool)
- Prefer small new module over growing `router.py` further for sticky store
- YAGNI: no Redis, no sticky persistence, no soft sticky-bonus ranking

---

## File Structure

| File | Responsibility |
|---|---|
| `session_sticky.py` | `SessionStickyStore`, `resolve_session_id(headers, body)` |
| `tests/test_session_sticky.py` | Store TTL/cap + session id resolution |
| `router.py` | Wire sticky into catalog order + chat loop; `CredentialPool` peek/prefer; remove RR/rotation/defaults; dashboard TBF split |
| `tests/test_catalog_routing.py` | Catalog order (no RR), sticky-first, peek no side-effect, pool preferred key |
| `documentation/routing.md`, `architecture.md`, `configuration.md`, `monitoring.md` | Docs |
| `website/src/content/docs/*.md` | Mirror docs |
| `.env.example` | Drop/retire `ROTATION_MODE` docs |

---

### Task 1: SessionStickyStore + resolve_session_id

**Files:**
- Create: `session_sticky.py`
- Create: `tests/test_session_sticky.py`

**Interfaces:**
- Produces:
  - `resolve_session_id(headers: dict, body: dict | None) -> str | None`
  - `class SessionStickyStore` with `__init__(self, ttl_s: float = 3600.0, max_entries: int = 10_000)`
  - `get(self, session_id: str) -> dict | None` — returns `{"provider", "model", "key", "updated_at"}` or `None` if missing/expired
  - `set(self, session_id: str, *, provider: str, model: str, key: str) -> None`
  - `clear(self, session_id: str) -> None`
  - `len(self) -> int` (for tests)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_session_sticky.py`:

```python
import time
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
    s = SessionStickyStore(ttl_s=10, max_entries=100)
    s.set("s1", provider="a", model="m", key="k")
    monkeypatch.setattr(time, "time", lambda: time.time() + 11)
    # Re-bind: store uses time.time internally — patch module time used by store
    import session_sticky as mod
    monkeypatch.setattr(mod.time, "time", lambda: 1_000_000.0)
    s2 = SessionStickyStore(ttl_s=10, max_entries=100)
    # Directly insert expired entry
    with s2._lock:
        s2._entries["s1"] = {
            "provider": "a", "model": "m", "key": "k", "updated_at": 1_000_000.0 - 11,
        }
    assert s2.get("s1") is None


def test_sticky_hard_cap_evicts_oldest():
    s = SessionStickyStore(ttl_s=3600, max_entries=2)
    s.set("a", provider="p", model="m", key="1")
    time.sleep(0.01)
    s.set("b", provider="p", model="m", key="2")
    time.sleep(0.01)
    s.set("c", provider="p", model="m", key="3")
    assert s.get("a") is None
    assert s.get("b") is not None and s.get("c") is not None
```

Fix the TTL test in the same file if the double-store approach is awkward — keep one clear TTL test that monkeypatches `session_sticky.time.time`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_session_sticky.py -v`  
Expected: FAIL (module not found / import error)

- [ ] **Step 3: Implement `session_sticky.py`**

```python
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
    def __init__(self, ttl_s: float = 3600.0, max_entries: int = 10_000):
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
            if time.time() - e["updated_at"] > self.ttl_s:
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
        expired = [k for k, e in self._entries.items()
                   if now - e["updated_at"] > self.ttl_s]
        for k in expired:
            del self._entries[k]
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries.items(), key=lambda kv: kv[1]["updated_at"])[0]
            del self._entries[oldest]
```

Simplify the TTL unit test to match this implementation (monkeypatch `session_sticky.time.time`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_session_sticky.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add session_sticky.py tests/test_session_sticky.py
git commit -m "$(cat <<'EOF'
feat: add SessionStickyStore and session id resolution

EOF
)"
```

---

### Task 2: CredentialPool preferred key + peek (drop ROTATION_MODE)

**Files:**
- Modify: `router.py` (`ROTATION_MODE` block ~146–151, `CredentialPool` ~1789–1887, startup log ~6698, `/v1/config/rotation` ~6213+, status `rotation` payload ~6512)
- Modify: `tests/test_catalog_routing.py` (create)

**Interfaces:**
- Consumes: existing `CredentialPool` pools shape
- Produces:
  - `peek_key(self, provider_name: str, model: str) -> str | None` — first ready key, **no** rotate, **no** `key_requests` bump
  - `get_key(self, provider_name: str, model: str, preferred: str | None = None) -> str | None` — if `preferred` ready, return it (no rotate); else first ready key (stable order, no RR/sequential modes)
  - Remove mode branching; if `ROTATION_MODE` env set at import, `log.warning` once that it is ignored

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_routing.py`:

```python
import router


def _pool_two_keys():
    providers = [{
        "name": "groq",
        "model": "llama",
        "models": ["llama"],
        "keys": ["key-aaaaaaa1", "key-bbbbbbb2"],
    }]
    return router.CredentialPool(providers)


def test_peek_key_does_not_advance_or_count():
    pool = _pool_two_keys()
    a = pool.peek_key("groq", "llama")
    b = pool.peek_key("groq", "llama")
    assert a == b == "key-aaaaaaa1"
    assert pool.key_requests_for("groq", "key-aaaaaaa1") == 0


def test_get_key_prefers_sticky_when_ready():
    pool = _pool_two_keys()
    k = pool.get_key("groq", "llama", preferred="key-bbbbbbb2")
    assert k == "key-bbbbbbb2"
    assert pool.key_requests_for("groq", "key-bbbbbbb2") == 1


def test_get_key_falls_back_when_preferred_cooling():
    pool = _pool_two_keys()
    pool.mark_key_down("groq", "key-bbbbbbb2", retry_after=60)
    k = pool.get_key("groq", "llama", preferred="key-bbbbbbb2")
    assert k == "key-aaaaaaa1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_catalog_routing.py::test_peek_key_does_not_advance_or_count tests/test_catalog_routing.py::test_get_key_prefers_sticky_when_ready tests/test_catalog_routing.py::test_get_key_falls_back_when_preferred_cooling -v`  
Expected: FAIL (`peek_key` missing / `preferred` ignored)

- [ ] **Step 3: Implement pool changes**

In `CredentialPool`:

1. Delete `self.mode` / `ROTATION_MODE` usage from `__init__` and `get_key`.
2. Add `peek_key` that scans the deque for first `cool_until <= now` without `rotate` or counting.
3. Change `get_key(..., preferred=None)`:
   - If preferred matches a ready entry → bump `key_requests`, return it (do not rotate).
   - Else return first ready entry (optional: rotate only when spreading is undesired — **do not rotate**; keep deque order stable for sticky-until-fail).
4. At module load where `ROTATION_MODE` was parsed: replace with:

```python
_raw_rotation = os.environ.get("ROTATION_MODE", "").strip()
if _raw_rotation:
    log.warning(f"ROTATION_MODE={_raw_rotation!r} is ignored; keys are sticky-until-fail")
```

5. Remove or stub `POST /v1/config/rotation` to return 410/400 with message that rotation mode was removed (prefer **delete endpoint** and dashboard control together in Task 5 if easier — for this task, make endpoint return JSON error `"rotation mode removed"` with 400 so old clients fail clearly).
6. Status payload: set `"rotation": {"mode": "sticky-key"}` or omit mode selector fields; keep key status list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_catalog_routing.py -v`  
Expected: PASS for the three pool tests (other tests in file may not exist yet)

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_catalog_routing.py
git commit -m "$(cat <<'EOF'
feat: sticky-until-fail keys; retire ROTATION_MODE selection

EOF
)"
```

---

### Task 3: Full-catalog order without provider RR; sticky-first; peek for headroom

**Files:**
- Modify: `router.py` (`_rr_counter` ~304, `_get_smart_ordered` ~1580–1643, status snapshot that calls `pool.get_key` ~6484)
- Modify: `tests/test_catalog_routing.py`

**Interfaces:**
- Consumes: `SessionStickyStore.get`, `CredentialPool.peek_key`, `rate_limiter.headroom`
- Produces: `_get_smart_ordered(..., sticky: dict | None = None) -> list` — no provider rotation; if sticky `{provider, model}` still in candidates, that candidate is first; headroom score uses `peek_key` not `get_key`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog_routing.py`:

```python
def test_smart_ordered_no_provider_rotation(monkeypatch):
    # Two equal-tier providers; without RR offset, order must be stable across calls
    p1 = {"name": "a", "model": "m1", "models": ["m1"], "keys": ["k1"]}
    p2 = {"name": "b", "model": "m1", "models": ["m1"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([p1, p2]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps", lambda n, m: {"rating": 5, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {"a": {"available": True}, "b": {"available": True}})
    o1 = [c["provider"]["name"] for c in router._get_smart_ordered([p1, p2], complexity=5)]
    o2 = [c["provider"]["name"] for c in router._get_smart_ordered([p1, p2], complexity=5)]
    assert o1 == o2


def test_smart_ordered_sticky_first(monkeypatch):
    p1 = {"name": "a", "model": "m1", "models": ["m1"], "keys": ["k1"]}
    p2 = {"name": "b", "model": "m2", "models": ["m2"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([p1, p2]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps", lambda n, m: {"rating": 5, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {"a": {"available": True}, "b": {"available": True}})
    sticky = {"provider": "b", "model": "m2", "key": "k2"}
    ordered = router._get_smart_ordered([p1, p2], complexity=5, sticky=sticky)
    assert ordered[0]["provider"]["name"] == "b"
    assert ordered[0]["model"] == "m2"
```

Adjust monkeypatches to match real `_model_caps` / `_price_rank` if tests flake (pin price ranks via monkeypatch if needed).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_catalog_routing.py::test_smart_ordered_no_provider_rotation tests/test_catalog_routing.py::test_smart_ordered_sticky_first -v`  
Expected: FAIL (RR still rotates / no `sticky` kwarg)

- [ ] **Step 3: Implement ordering changes**

1. Remove `_rr_counter` usage from `_get_smart_ordered` (delete counter if unused elsewhere).
2. Build candidates from `providers` in given order (no `offset` rotation).
3. Sort with existing `_key`, then if `sticky` provided, move matching `(provider.name, model)` to index 0 if present.
4. Replace `_peek_key = pool.get_key(...)` with `pool.peek_key(...)`.
5. In `/v1/status` rate_limits snapshot loop, use `peek_key` instead of `get_key` so status polling does not burn sticky/RR side effects.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_catalog_routing.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_catalog_routing.py
git commit -m "$(cat <<'EOF'
feat: full-catalog order without provider RR; sticky-first + peek

EOF
)"
```

---

### Task 4: Wire sticky into chat send path

**Files:**
- Modify: `router.py` (chat completions entry ~5320+, `_dispatch` / send loop ~5380–5750; instantiate store near `pool = CredentialPool(...)`)
- Modify: `tests/test_catalog_routing.py`

**Interfaces:**
- Consumes: `resolve_session_id`, `SessionStickyStore`, `_get_smart_ordered(..., sticky=...)`, `CredentialPool.get_key(..., preferred=...)`
- Produces: module-level `sticky_store = SessionStickyStore()`; chat path sets/clears sticky

- [ ] **Step 1: Write the failing integration-style unit test**

Append a focused test that mocks the send loop helpers **or** tests a extracted helper if you add one. Preferred minimal helper to keep router testable:

```python
# In router.py (new small helpers near sticky wiring):
# def _sticky_for_request(headers, body) -> tuple[str|None, dict|None]
# def _remember_sticky(session_id, provider, model, key) -> None
# def _clear_sticky(session_id) -> None
```

Test:

```python
def test_remember_and_clear_sticky():
    router.sticky_store = router.SessionStickyStore(ttl_s=3600, max_entries=100)
    router._remember_sticky("sess", "groq", "llama", "key1")
    assert router.sticky_store.get("sess")["key"] == "key1"
    router._clear_sticky("sess")
    assert router.sticky_store.get("sess") is None


def test_sticky_for_request_reads_header():
    sid, st = router._sticky_for_request(
        {"X-Hermes-Session-Id": "abc"}, {"messages": []})
    assert sid == "abc"
```

Then in the real send loop (Step 3), call these helpers — the loop wiring is verified by code review + a thinner test that `_get_smart_ordered` receives sticky from `_sticky_for_request` when building `ordered` (monkeypatch `_get_smart_ordered` to capture kwargs if needed).

- [ ] **Step 2: Run tests — expect fail until helpers exist**

Run: `pytest tests/test_catalog_routing.py::test_remember_and_clear_sticky tests/test_catalog_routing.py::test_sticky_for_request_reads_header -v`

- [ ] **Step 3: Implement helpers + wire chat loop**

1. `from session_sticky import SessionStickyStore, resolve_session_id`
2. `sticky_store = SessionStickyStore()`
3. Helpers as above.
4. In chat handler, after body parse:

```python
_session_id, _sticky = _sticky_for_request(request.headers, body)
ordered = _get_smart_ordered(..., sticky=_sticky)
```

5. Inner key loop:

```python
preferred = _sticky["key"] if (_sticky and _sticky.get("provider") == name
                               and _sticky.get("model") == model) else None
key = pool.get_key(name, model, preferred=preferred)
```

6. On **success** (non-stream and stream start paths that today record success): `_remember_sticky(_session_id, name, model, key)`.
7. When leaving a model after exhausting key attempts (end of `for _ in range(attempts)` without success) **or** when cascading due to 429/5xx that continues to next candidate: if sticky pointed at this `(name, model)`, `_clear_sticky(_session_id)` and set `_sticky = None` so failover does not keep preferring a dead model mid-request. On first success after clear, `_remember_sticky` writes the new winner.
8. Do **not** clear sticky merely because headroom is thin.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_catalog_routing.py tests/test_session_sticky.py tests/test_rate_limiter.py -q`  
Expected: PASS (rate limiter regression)

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_catalog_routing.py session_sticky.py
git commit -m "$(cat <<'EOF'
feat: session-sticky catalog routing on chat send path

EOF
)"
```

---

### Task 5: Remove default-model table / Reset; retire rotation API/UI

**Files:**
- Modify: `router.py` (`PROVIDER_MODEL_DEFAULT` ~883–897, `config_providers` defaults key, `config_model` DELETE, dashboard Models form Reset + default hint, Provider Keys rotation form, `setRotation` JS, `PAGES` / nav if needed)

**Interfaces:**
- Produces: no `PROVIDER_MODEL_DEFAULT`; `config_providers` omits `defaults` (or empty object); DELETE `/v1/config/model/<provider>` removed or returns 400 `"reset-to-default removed"`; POST model override kept

- [ ] **Step 1: Write failing API-oriented tests** (lightweight)

Append to `tests/test_catalog_routing.py`:

```python
def test_provider_model_default_removed():
    assert not hasattr(router, "PROVIDER_MODEL_DEFAULT") or router.PROVIDER_MODEL_DEFAULT == {}
```

Prefer **delete** the dict entirely and assert `hasattr(..., "PROVIDER_MODEL_DEFAULT") is False`.

- [ ] **Step 2: Run — expect fail while dict still exists**

- [ ] **Step 3: Implement removals**

1. Delete `PROVIDER_MODEL_DEFAULT` dict and comments.
2. `config_providers`: remove `"defaults"` key; update JS `onModelProviderChange` to not show `default: ...` / not compare against defaults — show current env/live model only; placeholder `"model or model1,model2,..."`.
3. Remove Reset button + `resetModel()`; remove DELETE handler **or** return 400 with clear message (prefer delete method from route: `methods=["POST"]` only).
4. Remove Key rotation `<select>` / Save rotation UI and `setRotation()`; delete `POST /v1/config/rotation` if still present.
5. Fix any CLI docs references in-repo that describe `hr mode round-robin` if this repo documents it (configuration.md in Task 7).

- [ ] **Step 4: Grep + tests**

Run: `rg -n "PROVIDER_MODEL_DEFAULT|resetModel|cfg-rotation|/v1/config/rotation" router.py`  
Expected: no matches (except maybe log strings).  
Run: `pytest tests/test_catalog_routing.py -q`

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_catalog_routing.py
git commit -m "$(cat <<'EOF'
feat: remove default-model reset and key rotation mode UI

EOF
)"
```

---

### Task 6: Move TBF UI to Providers + Models; remove Rate limits page

**Files:**
- Modify: `router.py` dashboard HTML/JS (`page-rate-limits`, nav, `renderProviders`, Models page, `refreshRateLimits`, `PAGES` array)

**Interfaces:**
- Consumes: existing `GET /v1/rate-limits?include_orphans=`
- Produces: Providers page section listing `scope === 'provider_wide'` rows; Models page section listing `scope === 'model'` rows; no `#page-rate-limits` / nav item

- [ ] **Step 1: Manual checklist as the “test” (plus smoke assert)**

Add a tiny test that the dashboard HTML string no longer contains `page-rate-limits` and does contain markers for both embeds:

```python
def test_dashboard_html_has_tbf_on_providers_and_models_not_standalone():
    html = router.DASHBOARD_HTML if hasattr(router, "DASHBOARD_HTML") else None
    # Dashboard is inline in router.py — read source or hit /dashboard in test client
    from router import app
    client = app.test_client()
    # /dashboard may need no auth — if gated, skip fetch and read file text
    src = open("router.py").read()
    assert 'id="page-rate-limits"' not in src
    assert "id=\"page-providers\"" in src and "id=\"page-models\"" in src
    assert "rl-tbody-provider" in src or "provider-wide" in src.lower()
    assert "rl-tbody-model" in src or "Token bucket" in src
```

Adjust marker ids you actually introduce (`rl-tbody-pw`, `rl-tbody-model`).

- [ ] **Step 2: Run test — expect fail while standalone page exists**

- [ ] **Step 3: Implement UI**

1. Remove nav button `Rate limits` and `<section id="page-rate-limits">`.
2. Remove `"rate-limits"` from `PAGES`.
3. On Providers page, after Advanced Provider Details (or inside it), add panel **Provider-wide token buckets** with table body `id="rl-tbody-pw"` and orphan checkbox `rl-orphans-pw`.
4. On Models page, add panel **Model token buckets** with `id="rl-tbody-model"` and orphan checkbox `rl-orphans-model`.
5. Split `refreshRateLimits` into filtering `rateLimitsData` by `g.scope === 'provider_wide'` vs `'model'` (reuse existing row renderer).
6. Keep detail modal + clear-group actions working from both tables.
7. Remove coarse single-model headroom bar from provider table **or** keep as summary — prefer keep min headroom summary on provider advanced table, detail on TBF panels.
8. Call both refreshers from `refresh()`.

- [ ] **Step 4: Run test + quick pytest**

Run: `pytest tests/test_catalog_routing.py::test_dashboard_html_has_tbf_on_providers_and_models_not_standalone -v`

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_catalog_routing.py
git commit -m "$(cat <<'EOF'
feat: embed TBF tables on Providers and Models pages

EOF
)"
```

---

### Task 7: Documentation + .env.example

**Files:**
- Modify: `documentation/routing.md`, `documentation/architecture.md`, `documentation/configuration.md`, `documentation/monitoring.md`
- Modify: `website/src/content/docs/routing.md`, `architecture.md`, `configuration.md`, `monitoring.md`
- Modify: `.env.example` (ROTATION_MODE section)
- Modify: `README.md` only if it advertises round-robin key mode as primary

**Interfaces:** none

- [ ] **Step 1: Update docs to match spec**

Required content:

- Chat routing: full-catalog selection; session-sticky `(provider, model, key)` until cascade-away; session id header/body priority; no session → fresh pick each request.
- Keys: sticky-until-fail; `ROTATION_MODE` ignored/removed.
- TBF: Providers page shows provider-wide; Models page shows model scope; standalone Rate limits page removed.
- Remove default-model Reset / `PROVIDER_MODEL_DEFAULT` docs; Save model override remains.
- Note Hermes Agent does not yet forward session id to custom endpoints.

- [ ] **Step 2: Grep leftovers**

Run: `rg -n "round-robin|ROTATION_MODE|Rate limits|PROVIDER_MODEL_DEFAULT|Reset.*default" documentation/ website/src/content/docs/ .env.example README.md`  
Fix remaining inaccurate lines.

- [ ] **Step 3: Commit**

```bash
git add documentation/ website/src/content/docs/ .env.example README.md
git commit -m "$(cat <<'EOF'
docs: session-sticky catalog routing and TBF dashboard placement

EOF
)"
```

---

### Task 8: Full verification

**Files:** none (run only)

- [ ] **Step 1: Run full relevant suite**

```bash
pytest tests/test_session_sticky.py tests/test_catalog_routing.py tests/test_rate_limiter.py tests/test_tool_routing.py tests/test_token_caps.py tests/test_token_caps_router.py tests/test_streaming_usage.py tests/test_specialized_models.py -q
```

Expected: all PASS

- [ ] **Step 2: Spec coverage smoke**

Confirm mentally against spec goals 1–6: no provider RR; sticky until cascade; router-only session ids; TBF on Providers/Models; defaults removed; ROTATION_MODE retired.

- [ ] **Step 3: Final commit only if verification fixed anything**; otherwise done.

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Full catalog, no provider RR | Task 3 |
| Sticky until cascade-away | Task 4 |
| Session id resolution priority / no invent | Task 1 |
| Sticky key until fail | Task 2 + 4 |
| TTL 3600 / cap 10_000 | Task 1 |
| Providers = health + provider-wide TBF; Models = model TBF | Task 6 |
| Remove Rate limits page | Task 6 |
| Remove default-model / Reset | Task 5 |
| Retire ROTATION_MODE | Task 2 + 5 + 7 |
| peek without get_key side effects | Task 2–3 |
| Docs | Task 7 |
| Embeddings non-sticky | Task 4 (chat only) |
| TBF semantics unchanged | Tasks avoid rate_limiter learning edits |

## Placeholder / consistency notes

- Use `CredentialPool` everywhere (not KeyPool).
- Sticky helper names: `_sticky_for_request`, `_remember_sticky`, `_clear_sticky`, module `sticky_store`.
- Dashboard tbody ids: `rl-tbody-pw`, `rl-tbody-model` (keep consistent in Task 6 test).
