# TTFT Baseline Load Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Abort upstream attempts whose time-to-first-byte exceeds a per-candidate EWMA baseline (with absolute floor / cold absolute), clear session affinity when that candidate was affine, and cascade on the same request — without breaker trips or lasting cooldowns.

**Architecture:** New `ttft_baseline.py` owns EWMA baselines and deadline math. `forward()` takes a first-byte read deadline, raises `TtftDeadlineExceeded` on `ReadTimeout` under that deadline, records successful header TTFT, then extends the socket read timeout for the rest of the body/stream. `_route_completion` computes the deadline per attempt, handles the abort path like a soft failure (release reservation, leave sticky, cascade reason `ttft_deadline`), and never calls `record_health(False)`.

**Tech Stack:** Python 3, pytest, Flask, `requests` (existing `_HTTP` session), existing cascade trail + session sticky.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-04-ttft-baseline-load-detection-design.md`
- Signal: TTFT only (headers-complete is the v1 measurement for `requests`; see Task 3)
- Grain: `(provider, model)` only
- Warm deadline: `max(TTFT_FLOOR_S, TTFT_MULT × ewma_s)`
- Cold deadline: `TTFT_COLD_DEADLINE_S` while `sample_count < TTFT_MIN_SAMPLES`
- Defaults: floor `3.0`, mult `3.0`, min samples `5`, cold `20.0`, α `0.2`, abort enabled `1`
- Abort never updates EWMA; only successful first-byte/header samples do
- TTFT abort must not trip circuit breaker, unsuitable cooling, or `skip_providers`
- No persistence, no shared cooldown, no hedging
- Glossary: Prefer Fallback / Cascade / Session affinity / Candidate in user-facing docs

## File Structure

| File | Responsibility |
|------|----------------|
| `ttft_baseline.py` | Env knobs, EWMA store, `deadline_s` / `record` / `summary`, `TtftDeadlineExceeded` |
| `tests/test_ttft_baseline.py` | Unit tests for deadline math and EWMA |
| `cascade_trail.py` | Label + priority for `ttft_deadline` |
| `tests/test_cascade_trail.py` | Reason label coverage |
| `router.py` | Wire store; `forward(..., first_byte_deadline_s=)`; cascade abort path |
| `tests/test_ttft_cascade.py` | Cascade sticky-clear + no health on TTFT abort |
| `.env.example` | Document TTFT_* knobs |
| `documentation/routing.md` | Session affinity + TTFT abort behavior |
| `documentation/configuration.md` | Env table for TTFT_* |
| `documentation/architecture.md` | One short pipeline note |

---

### Task 1: `TtftBaselineStore` module

**Files:**
- Create: `ttft_baseline.py`
- Create: `tests/test_ttft_baseline.py`

**Interfaces:**
- Consumes: env vars listed in the spec
- Produces:
  - `class TtftDeadlineExceeded(Exception)` with attributes `deadline_s: float`, `waited_s: float`
  - `class TtftBaselineStore` with:
    - `deadline_s(self, provider: str, model: str) -> float`
    - `record(self, provider: str, model: str, ttft_s: float) -> None`
    - `summary(self, provider: str, model: str) -> dict` → `{ewma_s, sample_count, last_ttft_s}` or empty defaults
  - Module-level helpers reading env once at import (or on each call via small `_float_env` / `_int_env` local copies matching `rate_limiter.py` style):
    - `TTFT_FLOOR_S`, `TTFT_MULT`, `TTFT_MIN_SAMPLES`, `TTFT_COLD_DEADLINE_S`, `TTFT_EWMA_ALPHA`, `TTFT_ABORT_ENABLED` (bool; `"0"`/`"false"`/`"no"`/`"off"` → False)
  - `abort_enabled() -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ttft_baseline.py
import ttft_baseline as mod
from ttft_baseline import TtftBaselineStore


def test_cold_deadline_until_min_samples(monkeypatch):
    monkeypatch.setenv("TTFT_COLD_DEADLINE_S", "20")
    monkeypatch.setenv("TTFT_MIN_SAMPLES", "5")
    monkeypatch.setenv("TTFT_FLOOR_S", "3")
    monkeypatch.setenv("TTFT_MULT", "3")
    # re-read knobs if module caches at import — either reload or construct store with explicit knobs
    s = TtftBaselineStore(floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=0.2)
    assert s.deadline_s("groq", "llama") == 20.0
    for i in range(4):
        s.record("groq", "llama", 2.0)
    assert s.deadline_s("groq", "llama") == 20.0  # still cold (n=4)


def test_warm_deadline_max_floor_and_mult_ewma():
    s = TtftBaselineStore(floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=1.0)
    for _ in range(5):
        s.record("a", "m", 1.0)  # ewma ≈ 1.0 with alpha=1
    # max(3.0, 3*1.0) = 3.0
    assert s.deadline_s("a", "m") == 3.0
    s.record("a", "m", 4.0)  # ewma=4 with alpha=1
    assert s.deadline_s("a", "m") == 12.0  # max(3, 3*4)


def test_ewma_smoothing_alpha():
    s = TtftBaselineStore(floor_s=0.0, mult=1.0, min_samples=1, cold_deadline_s=20.0, alpha=0.5)
    s.record("a", "m", 10.0)
    s.record("a", "m", 0.0)
    # ewma = 0.5*0 + 0.5*10 = 5
    assert abs(s.summary("a", "m")["ewma_s"] - 5.0) < 1e-9
    assert s.summary("a", "m")["sample_count"] == 2


def test_candidates_isolated():
    s = TtftBaselineStore(floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=1.0)
    for _ in range(5):
        s.record("groq", "fast", 1.0)
    assert s.deadline_s("groq", "slow") == 20.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ttft_baseline.py -v`  
Expected: FAIL (module / class missing)

- [ ] **Step 3: Implement `ttft_baseline.py`**

```python
"""Per-(provider, model) TTFT EWMA baselines and first-byte deadlines."""
from __future__ import annotations

import os
import threading
from typing import Any


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def abort_enabled() -> bool:
    return os.environ.get("TTFT_ABORT_ENABLED", "1").strip().lower() not in (
        "0", "", "false", "no", "off",
    )


class TtftDeadlineExceeded(Exception):
    def __init__(self, deadline_s: float, waited_s: float):
        self.deadline_s = float(deadline_s)
        self.waited_s = float(waited_s)
        super().__init__(
            f"TTFT deadline exceeded: waited {self.waited_s:.2f}s > {self.deadline_s:.2f}s"
        )


class TtftBaselineStore:
    def __init__(
        self,
        floor_s: float | None = None,
        mult: float | None = None,
        min_samples: int | None = None,
        cold_deadline_s: float | None = None,
        alpha: float | None = None,
    ):
        self.floor_s = float(floor_s if floor_s is not None else _float_env("TTFT_FLOOR_S", 3.0))
        self.mult = float(mult if mult is not None else _float_env("TTFT_MULT", 3.0))
        self.min_samples = int(min_samples if min_samples is not None else _int_env("TTFT_MIN_SAMPLES", 5))
        self.cold_deadline_s = float(
            cold_deadline_s if cold_deadline_s is not None else _float_env("TTFT_COLD_DEADLINE_S", 20.0)
        )
        self.alpha = float(alpha if alpha is not None else _float_env("TTFT_EWMA_ALPHA", 0.2))
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def _key(self, provider: str, model: str) -> tuple[str, str]:
        return (provider, model)

    def deadline_s(self, provider: str, model: str) -> float:
        with self._lock:
            e = self._data.get(self._key(provider, model))
            if not e or e["sample_count"] < self.min_samples:
                return self.cold_deadline_s
            return max(self.floor_s, self.mult * e["ewma_s"])

    def record(self, provider: str, model: str, ttft_s: float) -> None:
        ttft_s = float(ttft_s)
        if ttft_s < 0:
            return
        with self._lock:
            k = self._key(provider, model)
            e = self._data.get(k)
            if not e:
                self._data[k] = {
                    "ewma_s": ttft_s,
                    "sample_count": 1,
                    "last_ttft_s": ttft_s,
                }
                return
            a = self.alpha
            e["ewma_s"] = a * ttft_s + (1.0 - a) * e["ewma_s"]
            e["sample_count"] += 1
            e["last_ttft_s"] = ttft_s

    def summary(self, provider: str, model: str) -> dict:
        with self._lock:
            e = self._data.get(self._key(provider, model))
            if not e:
                return {"ewma_s": None, "sample_count": 0, "last_ttft_s": None}
            return {
                "ewma_s": e["ewma_s"],
                "sample_count": e["sample_count"],
                "last_ttft_s": e["last_ttft_s"],
            }
```

Constructor kwargs exist so unit tests do not need module reload; production uses env defaults.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ttft_baseline.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ttft_baseline.py tests/test_ttft_baseline.py
git commit -m "$(cat <<'EOF'
feat: add per-candidate TTFT EWMA baseline store

EOF
)"
```

---

### Task 2: Cascade reason `ttft_deadline`

**Files:**
- Modify: `cascade_trail.py` (`REASON_LABELS`, `_REASON_PRIORITY`)
- Modify: `tests/test_cascade_trail.py` (or add assertions there)

**Interfaces:**
- Consumes: none new
- Produces: `reason_label("ttft_deadline") == "TTFT deadline exceeded"` (exact string); priority similar to `network` (use `95` — below network `100`, above `http_5xx` `90`) so coalescing prefers real network only when both appear

- [ ] **Step 1: Write / extend failing test**

```python
from cascade_trail import reason_label, _prio

def test_ttft_deadline_label_and_priority():
    assert reason_label("ttft_deadline") == "TTFT deadline exceeded"
    assert _prio("ttft_deadline") == 95
    assert _prio("network") > _prio("ttft_deadline") > _prio("http_5xx")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cascade_trail.py::test_ttft_deadline_label_and_priority -v`  
Expected: FAIL (unknown label / wrong priority)

- [ ] **Step 3: Update `cascade_trail.py`**

Add to `REASON_LABELS`:

```python
"ttft_deadline": "TTFT deadline exceeded",
```

Add to `_REASON_PRIORITY`:

```python
"ttft_deadline": 95,
```

Also update the dashboard JS map in `router.py` if present (search for `network: 'Network / timeout'`) to include `ttft_deadline: 'TTFT deadline exceeded'` so the request-log modal shows a friendly label.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cascade_trail.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cascade_trail.py tests/test_cascade_trail.py router.py
git commit -m "$(cat <<'EOF'
feat: add ttft_deadline cascade reason label

EOF
)"
```

---

### Task 3: `forward()` first-byte deadline + TTFT record

**Files:**
- Modify: `router.py` (`forward`, imports, module-level `ttft_baselines = TtftBaselineStore()`)
- Create: `tests/test_ttft_forward.py`

**Interfaces:**
- Consumes: `TtftBaselineStore.record`, `TtftDeadlineExceeded`, `abort_enabled`
- Produces:
  - `forward(..., first_byte_deadline_s: float | None = None) -> requests.Response | None`
  - When `first_byte_deadline_s` is set: use `timeout=(10, first_byte_deadline_s)` on the upstream POST
  - On `requests.exceptions.ReadTimeout` while a first-byte deadline was set: raise `TtftDeadlineExceeded(deadline_s=first_byte_deadline_s, waited_s=elapsed)` (do **not** return `None`)
  - On other `RequestException` (including `ConnectTimeout`): return `None` as today (network path)
  - On successful response (headers received): `ttft_baselines.record(provider["name"], resolved_model, time.time() - t0)`, then `_extend_response_read_timeout(resp, 180.0)` (or 120 for anthropic default body), then return `resp`
  - When `first_byte_deadline_s is None`: keep today’s timeouts `(10, 120)` / `(10, 180)` for codex; **still** `record` TTFT on success when headers arrive (learning while abort disabled)
  - Helper `_extend_response_read_timeout(resp, seconds: float) -> None` best-effort socket bump so later stream chunks are not killed by the short TTFT read timeout

**v1 TTFT definition (explicit):** For `requests`, `POST(..., stream=...)` returns when **response headers** are complete. That elapsed time is recorded as TTFT. True first SSE body chunk is a follow-up if headers prove too early; do not block this task on it.

- [ ] **Step 1: Write failing tests** with mocked `_HTTP.post`

```python
# tests/test_ttft_forward.py
import time
from unittest.mock import MagicMock
import pytest
import requests
import router
from ttft_baseline import TtftDeadlineExceeded


def test_forward_read_timeout_raises_ttft(monkeypatch):
    store = router.ttft_baselines
    # ensure clean
    monkeypatch.setattr(router, "ttft_baselines", store.__class__(
        floor_s=3, mult=3, min_samples=5, cold_deadline_s=20, alpha=1.0))

    def boom(*a, **k):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(router._HTTP, "post", boom)
    provider = {"name": "groq", "base_url": "https://example.test/v1", "model": "m",
                "headers": {}}
    with pytest.raises(TtftDeadlineExceeded) as ei:
        router.forward(provider, "sk-test", {"messages": []}, False, "m",
                       first_byte_deadline_s=2.5)
    assert ei.value.deadline_s == 2.5
    assert router.ttft_baselines.summary("groq", "m")["sample_count"] == 0


def test_forward_success_records_ttft_and_extends(monkeypatch):
    monkeypatch.setattr(router, "ttft_baselines", router.TtftBaselineStore(
        floor_s=3, mult=3, min_samples=5, cold_deadline_s=20, alpha=1.0)
        if hasattr(router, "TtftBaselineStore") else __import__("ttft_baseline").TtftBaselineStore(
            floor_s=3, mult=3, min_samples=5, cold_deadline_s=20, alpha=1.0))

    resp = MagicMock()
    resp.status_code = 200
    called = {}

    def fake_post(*a, **k):
        called["timeout"] = k.get("timeout")
        time.sleep(0.01)
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    monkeypatch.setattr(router, "_extend_response_read_timeout",
                        lambda r, s: called.setdefault("extended", s))
    provider = {"name": "groq", "base_url": "https://example.test/v1", "model": "m",
                "headers": {}}
    out = router.forward(provider, "sk-test", {"messages": []}, False, "m",
                         first_byte_deadline_s=5.0)
    assert out is resp
    assert called["timeout"] == (10, 5.0)
    assert called["extended"] == 180.0
    assert router.ttft_baselines.summary("groq", "m")["sample_count"] == 1


def test_forward_connect_timeout_returns_none(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("nope")
    monkeypatch.setattr(router._HTTP, "post", boom)
    provider = {"name": "groq", "base_url": "https://example.test/v1", "model": "m",
                "headers": {}}
    assert router.forward(provider, "sk", {"messages": []}, False, "m",
                          first_byte_deadline_s=5.0) is None
```

(Adjust imports to match whatever `router.py` exports; prefer constructing a fresh `TtftBaselineStore` assigned to `router.ttft_baselines`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ttft_forward.py -v`  
Expected: FAIL (`first_byte_deadline_s` unexpected / no raise)

- [ ] **Step 3: Implement in `router.py`**

1. Import:

```python
from ttft_baseline import (
    TtftBaselineStore, TtftDeadlineExceeded, abort_enabled as ttft_abort_enabled,
)
ttft_baselines = TtftBaselineStore()
```

2. Add helper near `forward`:

```python
def _extend_response_read_timeout(resp, seconds: float) -> None:
    """Best-effort: after TTFT, allow long body/stream reads."""
    try:
        sock = getattr(getattr(getattr(resp, "raw", None), "_fp", None), "fp", None)
        raw = getattr(sock, "raw", None) if sock is not None else None
        s = getattr(raw, "_sock", None) if raw is not None else None
        if s is not None:
            s.settimeout(seconds)
            return
    except Exception:
        pass
    try:
        conn = getattr(resp, "connection", None)
        s = getattr(conn, "sock", None) if conn is not None else None
        if s is not None:
            s.settimeout(seconds)
    except Exception:
        pass
```

3. Change each `_HTTP.post(...)` inside `forward` to:
   - `t0 = time.time()` before post
   - choose `read_s = first_byte_deadline_s if first_byte_deadline_s is not None else <existing 120 or 180>`
   - `timeout=(10, read_s)`
   - on success: resolve model id used, `ttft_baselines.record(provider["name"], model_id, time.time() - t0)`, `_extend_response_read_timeout(resp, 180.0)` (use `120.0` if you want to match anthropic’s prior body timeout), return resp
   - except `requests.exceptions.ReadTimeout` if `first_byte_deadline_s is not None`: raise `TtftDeadlineExceeded(...)`
   - except other `RequestException`: log + return `None`

Apply to OpenAI-compatible, Anthropic, and Codex branches consistently.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_ttft_forward.py tests/test_ttft_baseline.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_ttft_forward.py
git commit -m "$(cat <<'EOF'
feat: honor TTFT first-byte deadline in forward()

EOF
)"
```

---

### Task 4: Cascade wiring — abort, sticky clear, no breaker

**Files:**
- Modify: `router.py` (`_route_completion` attempt loop ~6141–6157)
- Create: `tests/test_ttft_cascade.py`

**Interfaces:**
- Consumes: `ttft_baselines.deadline_s`, `ttft_abort_enabled`, `TtftDeadlineExceeded`, `_leave_sticky_model`
- Produces: on each attempt, before `forward`:
  - `deadline = ttft_baselines.deadline_s(name, model) if ttft_abort_enabled() else None`
  - `resp = forward(..., first_byte_deadline_s=deadline)`
  - `except TtftDeadlineExceeded as e:` → `_rl_release()`; log TTFT abort line with waited/deadline/ewma/n from `ttft_baselines.summary`; `_crec("note", name, model, "failed", "ttft_deadline")`; `_leave_sticky_model(name, model)`; **do not** `record_error` health? Spec: do not `record_health(False)`. Optionally still `stats.record_error` for observability — **prefer not** to inflate error_rate; skip both `record_error` and `record_health`. Do **not** `pool.mark_key_down`. `continue` to next key/candidate.
  - Existing `resp is None` network path unchanged (still health failure)

- [ ] **Step 1: Write failing cascade tests**

```python
# tests/test_ttft_cascade.py
from unittest.mock import MagicMock
import router
from ttft_baseline import TtftDeadlineExceeded
# reuse helpers pattern from tests/test_cascade_log.py (_stub_common, _two_candidates)


def test_ttft_abort_clears_sticky_and_cascades(monkeypatch):
    ordered, a, b = _two_candidates()  # copy helper locally or import if extracted
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(router, "ttft_abort_enabled", lambda: True)
    monkeypatch.setattr(router.ttft_baselines, "deadline_s", lambda p, m: 1.5)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    calls = {"n": 0}
    health = []

    def fake_forward(provider, key, payload, streaming, model, first_byte_deadline_s=None):
        calls["n"] += 1
        if provider["name"] == "prov_a":
            assert first_byte_deadline_s == 1.5
            raise TtftDeadlineExceeded(1.5, 1.5)
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    monkeypatch.setattr(router.stats, "record_health", lambda n, ok: health.append((n, ok)))
    router.sticky_store.set("sess1", provider="prov_a", model="m1", key="sk-a")

    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="ttft-cascade",
        _session_id="sess1",
        _sticky={"provider": "prov_a", "model": "m1", "key": "sk-a"},
    )
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s.get("reason") == "ttft_deadline" for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"
    assert ("prov_a", False) not in health  # no breaker trip from TTFT
    # _leave_sticky_model → sticky_store.clear; success on prov_b may re-set sticky
    assert router.sticky_store.get("sess1") is None or (
        router.sticky_store.get("sess1") or {}
    ).get("provider") == "prov_b"
```

Note: after cascade success on `prov_b`, sticky may be re-remembered to `prov_b` — assert affinity left `prov_a` (cleared or moved), never still stuck on `prov_a`.

Also add:

```python
def test_ttft_abort_disabled_passes_none_deadline(monkeypatch):
    ...
    monkeypatch.setattr(router, "ttft_abort_enabled", lambda: False)
    seen = {}
    def fake_forward(..., first_byte_deadline_s=None):
        seen["d"] = first_byte_deadline_s
        ...
    ...
    assert seen["d"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ttft_cascade.py -v`  
Expected: FAIL (forward signature / no handler)

- [ ] **Step 3: Wire `_route_completion`**

Replace:

```python
t0   = _rl_t0
resp = forward(provider, key, payload, streaming, model)
elapsed = time.time() - t0
```

with:

```python
t0 = _rl_t0
_deadline = ttft_baselines.deadline_s(name, model) if ttft_abort_enabled() else None
try:
    resp = forward(provider, key, payload, streaming, model,
                   first_byte_deadline_s=_deadline)
except TtftDeadlineExceeded as _ttft_ex:
    _rl_release()
    _sum = ttft_baselines.summary(name, model)
    log.warning(
        f"  {name}/{model} TTFT abort after {_ttft_ex.waited_s:.1f}s "
        f"(deadline {_ttft_ex.deadline_s:.1f}s, ewma {_sum.get('ewma_s')}, "
        f"n={_sum.get('sample_count', 0)})"
    )
    _crec("note", name, model, "failed", "ttft_deadline")
    _leave_sticky_model(name, model)
    continue
elapsed = time.time() - t0
```

Ensure `_rl_release` is defined before the try (it currently is defined after `forward` — move the nested `def _rl_release` above the try, or inline the release call).

Update any other `forward(...)` call sites only if they participate in chat cascade (embeddings stay unchanged).

- [ ] **Step 4: Run cascade + related tests**

Run: `pytest tests/test_ttft_cascade.py tests/test_cascade_log.py tests/test_ttft_forward.py tests/test_ttft_baseline.py tests/test_session_sticky.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_ttft_cascade.py
git commit -m "$(cat <<'EOF'
feat: cascade on TTFT deadline and clear session affinity

EOF
)"
```

---

### Task 5: Docs and env example

**Files:**
- Modify: `.env.example` (new commented TTFT_* block near breaker settings)
- Modify: `documentation/configuration.md` (advanced settings table)
- Modify: `documentation/routing.md` (session affinity paragraph — TTFT abort clears affinity)
- Modify: `documentation/architecture.md` (one sentence in fallback/pipeline)

**Interfaces:** none

- [ ] **Step 1: Update `.env.example`**

```bash
# TTFT load detection — abort slow first-byte and cascade (1=on)
# TTFT_ABORT_ENABLED=1
# TTFT_FLOOR_S=3.0          # never early-abort below this when warm
# TTFT_MULT=3.0             # abort if TTFT > mult × typical
# TTFT_MIN_SAMPLES=5        # samples before relative deadline
# TTFT_COLD_DEADLINE_S=20.0 # absolute wait until baseline is warm
# TTFT_EWMA_ALPHA=0.2       # EWMA smoothing for successful TTFT
```

- [ ] **Step 2: Docs**

In `routing.md` session affinity section, add that an exceeded TTFT deadline aborts the attempt, clears affinity, and cascades (load signal; not a breaker trip).

In `configuration.md` advanced table, add the six `TTFT_*` rows with defaults from the spec.

In `architecture.md`, note per-attempt TTFT deadline from EWMA baseline.

- [ ] **Step 3: Full regression**

Run: `pytest tests/ -q`  
Expected: PASS (or only pre-existing failures unrelated to TTFT — fix any new ones)

- [ ] **Step 4: Commit**

```bash
git add .env.example documentation/configuration.md documentation/routing.md documentation/architecture.md
git commit -m "$(cat <<'EOF'
docs: document TTFT baseline abort and env knobs

EOF
)"
```

---

## Spec coverage checklist

| Spec item | Task |
|-----------|------|
| Per-(provider, model) EWMA store | Task 1 |
| Cold absolute / warm floor+mult | Task 1 |
| Env knobs + abort feature flag | Tasks 1, 4, 5 |
| Record only on successful first byte | Tasks 1, 3 |
| Abort never records | Tasks 1, 3 |
| `forward` deadline + distinguish ReadTimeout | Task 3 |
| Extend read timeout after headers | Task 3 |
| Cascade `ttft_deadline` + sticky clear | Tasks 2, 4 |
| No breaker / unsuitable / skip_providers | Task 4 |
| No persistence / cooldown / hedging | All (omitted) |
| Docs | Task 5 |

## Self-review notes

- No TBD placeholders; defaults match the approved spec.
- `forward` signature gains `first_byte_deadline_s`; cascade tests must accept the kw-only-or-default param.
- Sticky clear uses existing `_leave_sticky_model` — same as token_cap / circuit paths.
- Headers-as-TTFT is an explicit v1 compromise documented in Task 3; do not expand scope to first SSE chunk in this plan.
