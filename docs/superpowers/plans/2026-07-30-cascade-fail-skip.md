# Cascade Fail/Skip Request Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Request-log entries record a structured cascade trail with separate failed vs skipped counts; the dashboard Fail/Skip cell opens a full-path detail modal (including the winner).

**Architecture:** New `cascade_trail.py` owns trail recording, reason-code priority for per-model coalescing, and count derivation. `_route_completion` and the embeddings loop append steps fail-soft onto `_req_ctx` / a local trail; `_log_completion` and embeddings `request_log.append` sites write `cascade`, `failed`, `skipped`, and widened `cascades` (`failed + skipped`). Dashboard JS renders both counts and a modal reused from the rate-limit overlay pattern.

**Tech Stack:** Python 3, Flask `router.py`, pytest, embedded dashboard HTML/CSS/JS in `router.py`, markdown under `documentation/` + `website/src/content/docs/`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-30-cascade-fail-skip-design.md`
- Observability only — **no** ranking / walk / headroom routing changes
- Rate headroom exhausted (no `forward`) → `skipped` / `rate_headroom`
- One trail step per `(provider, model)` outcome in a key loop (not per key); prefer most informative reason
- `success` step only when a winner exists; first-try win still gets a lone `success` step
- `cascades` = `failed + skipped` (widened vs old `attempts - 1`); prefer `failed`/`skipped` for new code
- Reasons are short stable codes — never raw response bodies
- Recording is fail-soft (must never break routing)
- Do not re-record candidates skipped only because `name in skip_providers` (noise); the step that added the provider already explains it
- `docs/superpowers/` is gitignored — use `git add -f` for plan/spec commits

---

## File Structure

| File | Responsibility |
|---|---|
| `cascade_trail.py` | `CascadeTrail`, reason codes, priority merge, `counts()`, `REASON_LABELS` |
| `tests/test_cascade_trail.py` | Unit tests for trail + counts + merge priority |
| `router.py` | Wire trail into `_route_completion`, `_log_completion`, embeddings appends; dashboard Fail/Skip + modal |
| `tests/test_cascade_log.py` | Integration: skip/fail/success steps appear on `_req_ctx` / log entry after `_route_completion` |
| `documentation/monitoring.md` | Document Fail/Skip + cascade fields |
| `website/src/content/docs/monitoring.md` | Mirror |

---

### Task 1: CascadeTrail helper

**Files:**
- Create: `cascade_trail.py`
- Create: `tests/test_cascade_trail.py`

**Interfaces:**
- Produces:
  - `REASON_LABELS: dict[str, str]` — human labels for known codes
  - `reason_label(code: str | None) -> str` — label or raw code or `""` for None
  - `http_reason(status_code: int) -> str` — maps status → `http_429` / `http_401` / … / `http_5xx` / `http_<n>`
  - `class CascadeTrail` with:
    - `steps: list[dict]` — each `{"provider", "model", "outcome", "reason"}`
    - `skip(self, provider: str, model: str, reason: str) -> None` — flush open attempt, append skipped step
    - `note(self, provider: str, model: str, outcome: str, reason: str) -> None` — coalesce into open step for current model (key-loop); higher-priority reason wins; outcome upgrades `skipped`→`failed` if both seen
    - `flush(self) -> None` — commit open step if any
    - `success(self, provider: str, model: str) -> None` — flush open, append success with `reason: None`
    - `as_log_fields(self) -> dict` — `{"cascade": [...], "failed": int, "skipped": int, "cascades": int}` after flush

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cascade_trail.py`:

```python
from cascade_trail import CascadeTrail, http_reason, reason_label


def test_http_reason_mapping():
    assert http_reason(429) == "http_429"
    assert http_reason(401) == "http_401"
    assert http_reason(403) == "http_403"
    assert http_reason(400) == "http_400"
    assert http_reason(404) == "http_404"
    assert http_reason(413) == "http_413"
    assert http_reason(503) == "http_5xx"
    assert http_reason(418) == "http_418"


def test_reason_label_known_and_unknown():
    assert "headroom" in reason_label("rate_headroom").lower()
    assert reason_label("totally_new") == "totally_new"
    assert reason_label(None) == ""


def test_skip_and_success_counts():
    t = CascadeTrail()
    t.skip("groq", "llama", "rate_headroom")
    t.skip("cerebras", "llama", "token_cap")
    t.success("openrouter", "gpt")
    fields = t.as_log_fields()
    assert fields["failed"] == 0
    assert fields["skipped"] == 2
    assert fields["cascades"] == 2
    assert [s["outcome"] for s in fields["cascade"]] == ["skipped", "skipped", "success"]
    assert fields["cascade"][-1]["reason"] is None


def test_note_coalesces_keys_preferring_informative_reason():
    t = CascadeTrail()
    t.note("groq", "llama", "skipped", "keys_cooling")
    t.note("groq", "llama", "skipped", "rate_headroom")
    t.note("groq", "llama", "failed", "http_429")
    t.flush()
    fields = t.as_log_fields()
    assert fields["failed"] == 1
    assert fields["skipped"] == 0
    assert fields["cascade"] == [
        {"provider": "groq", "model": "llama", "outcome": "failed", "reason": "http_429"}
    ]


def test_note_then_different_model_flushes():
    t = CascadeTrail()
    t.note("a", "m1", "failed", "network")
    t.note("a", "m2", "skipped", "rate_headroom")
    t.flush()
    assert len(t.as_log_fields()["cascade"]) == 2


def test_empty_trail():
    assert CascadeTrail().as_log_fields() == {
        "cascade": [], "failed": 0, "skipped": 0, "cascades": 0
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cascade_trail.py -v`

Expected: FAIL (module not found / import error)

- [ ] **Step 3: Implement `cascade_trail.py`**

```python
"""Structured cascade trail for request-log observability."""
from __future__ import annotations

REASON_LABELS = {
    "rate_headroom": "Rate headroom exhausted",
    "token_cap": "Input over token cap",
    "no_tools": "No tool support",
    "no_vision": "No vision support",
    "circuit_open": "Circuit breaker open",
    "access_scope": "Outside access-key provider scope",
    "keys_cooling": "All keys cooling",
    "network": "Network / timeout",
    "http_429": "HTTP 429",
    "http_401": "HTTP 401",
    "http_403": "HTTP 403",
    "http_400": "HTTP 400",
    "http_404": "HTTP 404",
    "http_413": "HTTP 413",
    "http_5xx": "HTTP 5xx",
}

# Higher wins when coalescing multiple key attempts on one model.
_REASON_PRIORITY = {
    "network": 100,
    "http_5xx": 90,
    "http_429": 80,
    "http_401": 70,
    "http_403": 70,
    "http_413": 65,
    "http_400": 60,
    "http_404": 60,
    "rate_headroom": 40,
    "keys_cooling": 20,
    "token_cap": 50,
    "no_tools": 50,
    "no_vision": 50,
    "circuit_open": 50,
    "access_scope": 50,
}


def reason_label(code: str | None) -> str:
    if code is None:
        return ""
    return REASON_LABELS.get(code, code)


def http_reason(status_code: int) -> str:
    known = {429, 401, 403, 400, 404, 413}
    if status_code in known:
        return f"http_{status_code}"
    if status_code >= 500:
        return "http_5xx"
    return f"http_{status_code}"


def _prio(reason: str) -> int:
    return _REASON_PRIORITY.get(reason, 10)


class CascadeTrail:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self._open: dict | None = None

    def skip(self, provider: str, model: str, reason: str) -> None:
        self.flush()
        self.steps.append({
            "provider": provider, "model": model,
            "outcome": "skipped", "reason": reason,
        })

    def note(self, provider: str, model: str, outcome: str, reason: str) -> None:
        if self._open and (self._open["provider"] != provider or self._open["model"] != model):
            self.flush()
        if self._open is None:
            self._open = {
                "provider": provider, "model": model,
                "outcome": outcome, "reason": reason,
            }
            return
        if outcome == "failed" and self._open["outcome"] != "failed":
            self._open["outcome"] = "failed"
        if _prio(reason) >= _prio(self._open["reason"]):
            self._open["reason"] = reason

    def flush(self) -> None:
        if self._open is not None:
            self.steps.append(self._open)
            self._open = None

    def success(self, provider: str, model: str) -> None:
        self.flush()
        self.steps.append({
            "provider": provider, "model": model,
            "outcome": "success", "reason": None,
        })

    def as_log_fields(self) -> dict:
        self.flush()
        failed = sum(1 for s in self.steps if s["outcome"] == "failed")
        skipped = sum(1 for s in self.steps if s["outcome"] == "skipped")
        return {
            "cascade": list(self.steps),
            "failed": failed,
            "skipped": skipped,
            "cascades": failed + skipped,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cascade_trail.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cascade_trail.py tests/test_cascade_trail.py
git commit -m "$(cat <<'EOF'
feat: add CascadeTrail helper for fail/skip request logging

EOF
)"
```

---

### Task 2: Wire trail into `_route_completion` + `_log_completion`

**Files:**
- Modify: `router.py` (imports; `_route_completion` init + skip/fail/success call sites; `_log_completion` fields)
- Create: `tests/test_cascade_log.py`

**Interfaces:**
- Consumes: `CascadeTrail`, `http_reason` from `cascade_trail`
- Produces: `_req_ctx.cascade: CascadeTrail` (created when `_rate_retry` is false); log entries from `_log_completion` include `cascade`, `failed`, `skipped`, `cascades`

**Recording map (chat/messages loop in `_route_completion`):**

| Branch | Call |
|---|---|
| access scope skip | `trail.skip(name, model, "access_scope")` |
| circuit open | `trail.skip(name, model, "circuit_open")` |
| token cap | `trail.skip(name, model, "token_cap")` |
| no tools (main phase defer) | `trail.skip(name, model, "no_tools")` |
| no vision | `trail.skip(name, model, "no_vision")` |
| `name in skip_providers` | **do not record** |
| no key (`break` with no successful forward) | before leaving candidate: if no open failed note yet, `trail.note(name, model, "skipped", "keys_cooling")` then rely on flush at end of candidate |
| rate headroom exhausted (`continue` without forward) | `trail.note(name, model, "skipped", "rate_headroom")` |
| `resp is None` | `trail.note(name, model, "failed", "network")` |
| HTTP error statuses | `trail.note(name, model, "failed", http_reason(status))` |
| success before return | `trail.success(name, model)` |
| end of candidate (after key loop, before `_leave_sticky_model`) | `trail.flush()` so a model with only key-loop notes is committed |

**Rate-retry recursion:** only construct a new trail when `not _rate_retry`; on retry keep `_req_ctx.cascade` and append further steps.

**`_log_completion`:** replace `cascades: max(0, attempts - 1)` with fields from `getattr(_req_ctx, "cascade", None)`. If missing/empty trail and cache hit → empty fields. If somehow missing on a non-cache path, fall back to `failed=max(0,attempts-1), skipped=0, cascade=[], cascades=failed` so logging never raises.

- [ ] **Step 1: Write the failing integration tests**

Create `tests/test_cascade_log.py` (pattern from `tests/test_tool_routing.py` — uses `_ordered_providers`):

```python
from unittest.mock import MagicMock
import router
from cascade_trail import CascadeTrail


def _two_candidates():
    a = {"name": "prov_a", "base_url": "https://a.test/v1", "model": "m1",
         "models": ["m1"], "keys": ["sk-a"]}
    b = {"name": "prov_b", "base_url": "https://b.test/v1", "model": "m2",
         "models": ["m2"], "keys": ["sk-b"]}
    return (
        [{"provider": a, "model": "m1"}, {"provider": b, "model": "m2"}],
        a, b,
    )


def _stub_common(monkeypatch):
    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router, "_estimated_tokens", lambda m: 10)
    monkeypatch.setattr(router, "_effective_input_cap_for", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router, "_completion_has_output", lambda d: True)
    monkeypatch.setattr(router, "_strip_response", lambda d: None)
    monkeypatch.setattr(router, "_add_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router, "_learn_token_cap_from_success", lambda **k: None)
    monkeypatch.setattr(router, "_learn_token_cap_from_error", lambda **k: None)
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "check_and_consume",
                        lambda *a, **k: (True, 0.0))
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router.rate_limiter, "release_reservation", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_429", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "restore", lambda *a, **k: None)

    class _Pool:
        def key_count(self, name, model):
            return 1
        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"
        def mark_key_down(self, *a, **k):
            pass
        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())


def test_token_cap_skip_then_success(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        router, "_effective_input_cap_for",
        lambda provider, model: 5 if provider["name"] == "prov_a" else None,
    )
    monkeypatch.setattr(router, "_estimated_tokens", lambda m: 100)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        assert provider["name"] == "prov_b"
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-cascade-cap")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert fields["cascade"][0]["outcome"] == "skipped"
    assert fields["cascade"][0]["reason"] == "token_cap"
    assert fields["cascade"][0]["provider"] == "prov_a"
    assert fields["cascade"][-1]["outcome"] == "success"
    assert fields["skipped"] == 1
    assert fields["failed"] == 0
    assert fields["cascades"] == 1


def test_http_429_then_success(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    calls = {"n": 0}
    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        calls["n"] += 1
        resp = MagicMock()
        resp.headers = {}
        if calls["n"] == 1:
            resp.status_code = 429
            resp.text = "rate"
            return resp
        resp.status_code = 200
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-cascade-429")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s["outcome"] == "failed" and s["reason"] == "http_429"
               for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"
    assert fields["failed"] >= 1
    assert fields["cascades"] == fields["failed"] + fields["skipped"]


def test_rate_headroom_skip_classified_as_skipped(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)

    def fake_check(name, key, model, req_count=1.0, token_count=1.0):
        if name == "prov_a":
            return False, 60.0
        return True, 0.0

    monkeypatch.setattr(router.rate_limiter, "check_and_consume", fake_check)
    monkeypatch.setattr(router.rate_limiter, "headroom",
                        lambda name, key, model: 0.0 if name == "prov_a" else 1.0)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        assert provider["name"] == "prov_b"
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-cascade-rl")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s["outcome"] == "skipped" and s["reason"] == "rate_headroom"
               for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"


def test_log_completion_includes_cascade_fields():
    trail = CascadeTrail()
    trail.skip("groq", "m", "rate_headroom")
    trail.success("mistral", "m2")
    router._req_ctx.cascade = trail
    router._req_ctx.cache_hit = False
    router._req_ctx.attempts = 1
    router._req_ctx.provider = "mistral"
    router._req_ctx.model = "m2"
    router._req_ctx.last_tried_provider = "mistral"
    router._req_ctx.last_tried_model = "m2"
    entry = router._log_completion(
        "sk-test-xxxxxx", "chat",
        {"messages": [{"role": "user", "content": "x"}], "stream": False},
        ("json", {"usage": {}}), 0.01)
    assert entry is not None
    assert entry["failed"] == 0
    assert entry["skipped"] == 1
    assert entry["cascades"] == 1
    assert len(entry["cascade"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cascade_log.py -v`

Expected: FAIL (no cascade on `_req_ctx` / missing log fields)

- [ ] **Step 3: Wire `router.py`**

1. Import: `from cascade_trail import CascadeTrail, http_reason`
2. At start of `_route_completion` (near existing `_req_ctx.attempts = 0`):

```python
if not _rate_retry:
    _req_ctx.cascade = CascadeTrail()
trail = _req_ctx.cascade
```

3. At each skip/fail/success branch per the recording map above. Wrap each call in try/except that logs at debug and continues (fail-soft), or make `CascadeTrail` methods never raise and trust that.
4. On success path immediately before returning stream/json: `trail.success(name, model)`
5. After the key `for` loop finishes for a candidate (the existing `_leave_sticky_model` / `_queue_tool_last_resort` site): `trail.flush()`
6. In `_log_completion`:

```python
_cascade = getattr(_req_ctx, "cascade", None)
if isinstance(_cascade, CascadeTrail):
    _fields = _cascade.as_log_fields()
else:
    _failed = max(0, attempts - 1)
    _fields = {"cascade": [], "failed": _failed, "skipped": 0, "cascades": _failed}
# merge _fields into entry; do not set cascades from attempts-1 anymore
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cascade_trail.py tests/test_cascade_log.py tests/test_tool_routing.py tests/test_catalog_routing.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_cascade_log.py
git commit -m "$(cat <<'EOF'
feat: record fail/skip cascade trail on chat request logs

EOF
)"
```

---

### Task 3: Embeddings cascade trail

**Files:**
- Modify: `router.py` (`embeddings()` function — three `request_log.append` sites ~6136, ~6238, ~6257)
- Modify: `tests/test_cascade_log.py` (add one embeddings test)

**Interfaces:**
- Consumes: `CascadeTrail`, `http_reason`
- Produces: embeddings log entries always include `cascade`/`failed`/`skipped`/`cascades` (empty on cache hit)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cascade_log.py`:

```python
def test_embeddings_cache_hit_has_empty_cascade_fields(monkeypatch):
    router.request_log.clear()
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: {"data": [], "object": "list"})
    monkeypatch.setattr(router, "_auth_check", lambda: None)
    monkeypatch.setattr(router, "_admit_request", lambda token: None)
    monkeypatch.setattr(router, "_caller_token", lambda: "sk-test-xxxxxx")
    monkeypatch.setattr(router, "_embed_ordered", lambda: [
        {"name": "gemini", "embed_model": "text-embedding-004", "keys": ["k"]},
    ])
    client = router.app.test_client()
    r = client.post("/v1/embeddings",
                    json={"input": "hello", "model": "text-embedding-004"},
                    headers={"Authorization": "Bearer sk-test-xxxxxx"})
    assert r.status_code == 200
    entries = router.request_log.snapshot(limit=5)
    assert entries
    e = entries[-1]
    assert e["status"] == "cache_hit"
    assert e["cascade"] == []
    assert e["failed"] == 0
    assert e["skipped"] == 0
    assert e["cascades"] == 0


def test_embeddings_success_after_headroom_skip_counts_skipped(monkeypatch):
    router.request_log.clear()
    p_a = {"name": "emb_a", "embed_model": "e1", "keys": ["ka"],
           "base_url": "https://a.test/v1"}
    p_b = {"name": "emb_b", "embed_model": "e2", "keys": ["kb"],
           "base_url": "https://b.test/v1"}
    monkeypatch.setattr(router, "_auth_check", lambda: None)
    monkeypatch.setattr(router, "_admit_request", lambda token: None)
    monkeypatch.setattr(router, "_caller_token", lambda: "sk-test-xxxxxx")
    monkeypatch.setattr(router, "_embed_ordered", lambda: [p_a, p_b])
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(router, "_add_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router.key_usage, "add_tokens", lambda *a, **k: None)

    def fake_check(name, key, model, req_count=1.0, token_count=1.0):
        if name == "emb_a":
            return False, 60.0
        return True, 0.0

    monkeypatch.setattr(router.rate_limiter, "check_and_consume", fake_check)
    monkeypatch.setattr(router.rate_limiter, "headroom",
                        lambda name, key, model: 0.0 if name == "emb_a" else 1.0)
    monkeypatch.setattr(router.rate_limiter, "release_reservation", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)

    class _Pool:
        def key_count(self, name, model):
            return 1
        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"
        def mark_key_down(self, *a, **k):
            pass

    monkeypatch.setattr(router, "pool", _Pool())

    def fake_fwd(provider, key, payload):
        assert provider["name"] == "emb_b"
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {
            "data": [{"embedding": [0.1]}],
            "usage": {"total_tokens": 3},
        }
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward_embeddings", fake_fwd)
    client = router.app.test_client()
    r = client.post("/v1/embeddings",
                    json={"input": "hello", "model": "text-embedding-004"},
                    headers={"Authorization": "Bearer sk-test-xxxxxx"})
    assert r.status_code == 200
    e = router.request_log.snapshot(limit=5)[-1]
    assert e["status"] == "success"
    assert e["skipped"] >= 1
    assert any(s["reason"] == "rate_headroom" for s in e["cascade"])
    assert e["cascade"][-1]["outcome"] == "success"
    assert e["cascades"] == e["failed"] + e["skipped"]
```

Confirm `RequestRingBuffer.clear()` exists (it does — clears `_buf`). If auth still blocks the test client, also monkeypatch `PROXY_API_KEYS` / whatever `_auth_check` uses so Bearer `sk-test-xxxxxx` is accepted — match patterns from other Flask client tests in the repo if present.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cascade_log.py -k embeddings -v`

Expected: FAIL (missing fields / cascades still 0 after cascade)

- [ ] **Step 3: Implement embeddings wiring**

In `embeddings()`:

```python
trail = CascadeTrail()
```

- Cache-hit append: merge `**trail.as_log_fields()` (empty).
- Circuit open / rate headroom / keys cooling / HTTP failures: `trail.skip` / `trail.note` same codes as chat.
- On success: `trail.success(name, em)` then `request_log.append({..., **trail.as_log_fields()})` — remove hardcoded `"cascades": 0`.
- Exhausted error append: `**trail.as_log_fields()` (may contain skipped/failed, no success).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cascade_log.py tests/test_cascade_trail.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_cascade_log.py
git commit -m "$(cat <<'EOF'
feat: record cascade trail on embeddings request logs

EOF
)"
```

---

### Task 4: Dashboard Fail/Skip column + cascade modal

**Files:**
- Modify: `router.py` (`_DASHBOARD_HTML` — table header, `renderLogs`, modal markup/CSS/JS, Escape handler)

**Interfaces:**
- Consumes: log entry fields `failed`, `skipped`, `cascades`, `cascade`
- Produces: clickable Fail/Skip cell; `#cascade-detail-modal` listing full path

- [ ] **Step 1: Update table header**

In the Request Log `<thead>`, replace Cascades with:

```html
<th class="right">Fail / Skip</th>
```

- [ ] **Step 2: Add cascade modal markup + CSS**

Near `#rl-detail-modal`, add:

```html
<div id="cascade-detail-modal" class="hidden" onclick="if(event.target===this)closeCascadeDetail()">
  <div class="rl-detail-box panel" role="dialog" aria-modal="true" aria-labelledby="cascade-detail-title">
    <div class="panel-header">
      <span class="panel-title" id="cascade-detail-title">Cascade</span>
      <button class="btn" onclick="closeCascadeDetail()">Close</button>
    </div>
    <div class="panel-body">
      <div id="cascade-detail-meta" class="muted" style="padding:8px 12px;font-size:12px"></div>
      <table>
        <thead><tr>
          <th>#</th><th>Provider</th><th>Model</th><th>Outcome</th><th>Reason</th>
        </tr></thead>
        <tbody id="cascade-detail-tbody"></tbody>
      </table>
    </div>
  </div>
</div>
```

Reuse `#rl-detail-modal` CSS (shared `.rl-detail-box`). Add `#cascade-detail-modal` with the same overlay rules as `#rl-detail-modal` (copy the four rules, swap the id).

- [ ] **Step 3: Implement JS render + open/close**

Add reason label map (mirror `REASON_LABELS`):

```javascript
const CASCADE_REASON_LABELS = {
  rate_headroom: 'Rate headroom exhausted',
  token_cap: 'Input over token cap',
  no_tools: 'No tool support',
  no_vision: 'No vision support',
  circuit_open: 'Circuit breaker open',
  access_scope: 'Outside access-key provider scope',
  keys_cooling: 'All keys cooling',
  network: 'Network / timeout',
  http_429: 'HTTP 429',
  http_401: 'HTTP 401',
  http_403: 'HTTP 403',
  http_400: 'HTTP 400',
  http_404: 'HTTP 404',
  http_413: 'HTTP 413',
  http_5xx: 'HTTP 5xx',
};
function cascadeReasonLabel(code) {
  if (code == null || code === '') return '—';
  return CASCADE_REASON_LABELS[code] || code;
}
```

Replace `cascBadge` in `renderLogs()`:

```javascript
const hasTrail = Array.isArray(e.cascade) && e.cascade.length > 0;
const failed = e.failed != null ? e.failed : null;
const skipped = e.skipped != null ? e.skipped : null;
let cascCell;
if (failed != null && skipped != null) {
  const nums = `<span class="${(failed+skipped)>0?'':'muted'}">${failed} / ${skipped}</span>`;
  cascCell = hasTrail
    ? `<button type="button" class="linkish" onclick='openCascadeDetail(${JSON.stringify(e).replace(/'/g, "&#39;")})'>${nums}</button>`
    : nums;
} else {
  // Legacy mid-deploy entries
  cascCell = e.cascades > 0
    ? `<span class="pill pill-warn">${e.cascades}</span>`
    : '<span class="muted">0</span>';
}
```

**Safer click wiring (preferred over inline JSON):** store entries by index:

```javascript
// in renderLogs loop:
const idx = /* index in logsData */;
const clickable = hasTrail
  ? `style="cursor:pointer" onclick="openCascadeDetail(${idx})"`
  : '';
// cell: <td class="right" ${clickable}>...</td>
```

```javascript
function openCascadeDetail(idx) {
  const e = logsData[idx];
  if (!e || !Array.isArray(e.cascade) || !e.cascade.length) return;
  document.getElementById('cascade-detail-title').textContent =
    `Cascade · ${e.endpoint || '—'} · ${e.status || '—'}`;
  document.getElementById('cascade-detail-meta').textContent =
    `${fmt.time(e.ts)} · fail ${e.failed||0} / skip ${e.skipped||0}`;
  const outcomePill = (o) => {
    const cls = o === 'success' ? 'pill-ok' : o === 'failed' ? 'pill-err' : 'pill-warn';
    return `<span class="pill ${cls}">${o}</span>`;
  };
  document.getElementById('cascade-detail-tbody').innerHTML = e.cascade.map((s, i) => `
    <tr>
      <td class="muted">${i+1}</td>
      <td><strong>${s.provider||'—'}</strong></td>
      <td class="mono muted">${s.model||'—'}</td>
      <td>${outcomePill(s.outcome)}</td>
      <td class="muted">${s.outcome==='success'?'—':cascadeReasonLabel(s.reason)}</td>
    </tr>`).join('');
  document.getElementById('cascade-detail-modal').classList.remove('hidden');
}
function closeCascadeDetail() {
  document.getElementById('cascade-detail-modal').classList.add('hidden');
}
```

Update Escape handler to also close `#cascade-detail-modal`.

Add minimal `.linkish` or rely on `cursor:pointer` on the `<td>` — no new card chrome.

Update empty-state `colspan` if column count unchanged (still 10).

- [ ] **Step 4: Manual check**

Restart the router if needed. Open `/dashboard` → Request Log. Trigger a request that cascades (or inspect existing entries after Task 2/3). Confirm:

1. Cell shows `F / S` numbers
2. Click opens ordered trail including winner
3. Escape / backdrop closes modal
4. Legacy-shaped entry (if any) still renders

- [ ] **Step 5: Commit**

```bash
git add router.py
git commit -m "$(cat <<'EOF'
feat: show fail/skip cascade detail in request log dashboard

EOF
)"
```

---

### Task 5: Docs

**Files:**
- Modify: `documentation/monitoring.md` (Request log section ~231–241; dashboard bullet ~33–36)
- Modify: `website/src/content/docs/monitoring.md` (same edits)

- [ ] **Step 1: Update monitoring docs**

Replace cascade-count wording with:

- Dashboard live log shows **Fail / Skip** counts; click opens the full cascade path (skipped / failed / success) with reason codes.
- Each `/v1/logs` entry includes `failed`, `skipped`, `cascades` (`failed + skipped`), and `cascade` (ordered steps: `provider`, `model`, `outcome`, `reason`).
- Note that `cascades` now includes skips (widened vs older builds that counted only failed forwards).

Keep “content is never stored”.

- [ ] **Step 2: Mirror to website doc**

Apply the same edits to `website/src/content/docs/monitoring.md`.

- [ ] **Step 3: Commit**

```bash
git add documentation/monitoring.md website/src/content/docs/monitoring.md
git commit -m "$(cat <<'EOF'
docs: describe fail/skip cascade fields in request log

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Separate failed vs skipped | 1–2 |
| Rate headroom → skipped | 2 (recording map) |
| Both numbers in table | 4 |
| Single click → full path incl. winner | 4 |
| Structured trail on log entry | 2–3 |
| Per-model not per-key coalesce | 1 (`note`) |
| No routing changes | Global constraint |
| Fail-soft recording | 2 |
| Legacy entry fallback UI | 4 |
| Embeddings trail | 3 |
| Docs monitoring + website mirror | 5 |
| No VS Code / Prometheus / raw bodies | Non-goals (no tasks) |

## Self-review notes

- Spec sections mapped to tasks in the checklist above; no uncovered goals.
- No TBD / placeholder test bodies; Task 2–3 use full fixtures mirroring `tests/test_tool_routing.py`.
- `http_reason` / `CascadeTrail` / log field names are consistent across tasks.
- Rate-retry keeps the same trail instance (`not _rate_retry` gate).
- Dashboard uses index-based `openCascadeDetail(idx)` (not inline JSON) to avoid XSS/escaping issues.
