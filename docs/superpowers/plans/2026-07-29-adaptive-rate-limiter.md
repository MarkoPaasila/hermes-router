# Adaptive Token Bucket Rate Limiter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-adjusting multi-window token bucket filter to hermes-router that learns upstream provider rate limits and uses them for both proactive request pacing and headroom-aware routing.

**Architecture:** A new `AdaptiveRateLimiter` class (in a new `rate_limiter.py` module) holds `BucketGroup` instances keyed by `(provider_name, api_key_suffix, model_or_None)`. Each group owns up to 10 `TokenBucket` objects (RPM/RPH/RPD/RPW/RPMo + TPM/TPH/TPD/TPW/TPMo). The limiter integrates into `router.py` at three points: routing-score injection in `_get_smart_ordered`, send-time gate in the try-loop, and learning-loop update after each response. State is persisted to `rate_limits_state.json` by a background flush thread.

**Tech Stack:** Python 3.10+, threading (already used throughout), json (stdlib), existing Flask/Waitress server — no new dependencies.

## Global Constraints

- Single-file router (`router.py`) is the primary integration target; do not split it.
- New module `rate_limiter.py` lives alongside `router.py`.
- All env var names are `RATE_*` (see spec §10).
- Key material in state-file group keys is truncated to the last 8 characters of the API key.
- No new pip dependencies.
- Tests go in `tests/test_rate_limiter.py`; run with `pytest tests/`.
- Existing `mark_rate_limited` behaviour is preserved — the adaptive limiter supplements it, never replaces it.

---

### Task 1: TokenBucket — core mechanics

**Files:**
- Create: `rate_limiter.py`
- Create: `tests/test_rate_limiter.py`

**Interfaces:**
- Produces:
  - `class TokenBucket(window_seconds: float, cap: float, tokens: float = None, last_refill: float = None)`
    - `window_seconds: float` — stored attribute
    - `cap: float` — stored attribute, mutable
    - `tokens: float` — stored attribute
    - `active: bool` — stored attribute, default `True`
    - `_period_consumed: float` — running total consumed this period, for 429-cut
    - `_consecutive_successes: int` — streak counter for nudge
    - `refill(now: float) -> None` — add `elapsed * (cap / window_seconds)` to tokens, clamp to cap, update last_refill
    - `consume(amount: float) -> bool` — call `refill(time.time())`, return True and debit if tokens >= amount, else False
    - `restore(amount: float) -> None` — add amount back, clamp to cap
    - `headroom() -> float` — `tokens / cap` clamped 0.0–1.0; returns 1.0 if cap <= 0
    - `time_to_refill(amount: float) -> float` — seconds until `tokens >= amount` at current refill rate; 0.0 if already enough
    - `on_success() -> None` — increment `_consecutive_successes`; if >= `RATE_LEARN_SUCCESS_STREAK` and cap < initial_cap * `RATE_LEARN_MAX_MULTIPLIER`: multiply cap by `(1 + RATE_LEARN_NUDGE_PCT/100)`, reset streak
    - `on_429(observed_rate: float) -> None` — if `_period_consumed >= 3`: set cap to `max(1.0, observed_rate * RATE_LEARN_CUT_FACTOR)`; else cap = `max(1.0, cap * 0.5)`; set tokens = 0; reset streak
    - `set_from_header(cap: float, remaining: float) -> None` — overwrite cap and tokens directly
    - `check_inactive(requests_this_period: int) -> None` — if never hit 0 tokens (`_hit_zero` flag False) and `requests_this_period < max(10, cap * 0.1)`: set `active = False`
    - `to_dict() -> dict` — `{"cap": ..., "tokens": ..., "last_refill": ...}`
    - `classmethod from_dict(d: dict, window_seconds: float, initial_cap: float) -> TokenBucket`

- [ ] **Step 1: Write failing tests for TokenBucket**

```python
# tests/test_rate_limiter.py
import time, pytest
from rate_limiter import TokenBucket

WINDOW = 60.0  # 1-minute bucket

def make_bucket(cap=10.0, tokens=None):
    return TokenBucket(window_seconds=WINDOW, cap=cap,
                       tokens=cap if tokens is None else tokens)

def test_consume_success():
    b = make_bucket(cap=10.0, tokens=10.0)
    assert b.consume(1.0) is True
    assert abs(b.tokens - 9.0) < 0.01

def test_consume_fails_when_empty():
    b = make_bucket(cap=10.0, tokens=0.0)
    assert b.consume(1.0) is False

def test_refill_adds_tokens():
    b = make_bucket(cap=60.0, tokens=0.0)
    b.last_refill = time.time() - 30  # 30s elapsed
    b.refill(time.time())
    assert abs(b.tokens - 30.0) < 0.5   # 30s * (60/60) = 30

def test_refill_clamps_to_cap():
    b = make_bucket(cap=10.0, tokens=9.0)
    b.last_refill = time.time() - 100
    b.refill(time.time())
    assert b.tokens == 10.0

def test_headroom():
    b = make_bucket(cap=10.0, tokens=5.0)
    assert abs(b.headroom() - 0.5) < 0.01

def test_headroom_zero_cap():
    b = make_bucket(cap=0.0, tokens=0.0)
    assert b.headroom() == 1.0

def test_time_to_refill_already_enough():
    b = make_bucket(cap=10.0, tokens=5.0)
    assert b.time_to_refill(3.0) == 0.0

def test_time_to_refill_calculates():
    b = make_bucket(cap=60.0, tokens=0.0)
    # rate = 60/60 = 1 tok/s; need 10 more → 10s
    assert abs(b.time_to_refill(10.0) - 10.0) < 0.1

def test_on_429_with_history_cuts_cap():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0)
    assert b.cap == pytest.approx(8.0)   # 10 * 0.8
    assert b.tokens == 0.0

def test_on_429_without_history_halves():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 1.0
    b.on_429(observed_rate=1.0)
    assert b.cap == pytest.approx(30.0)  # cap * 0.5

def test_on_success_nudge():
    b = make_bucket(cap=10.0, tokens=10.0)
    b._initial_cap = 10.0
    for _ in range(20):
        b.on_success()
    assert b.cap > 10.0

def test_set_from_header():
    b = make_bucket(cap=10.0, tokens=5.0)
    b.set_from_header(cap=100.0, remaining=80.0)
    assert b.cap == 100.0
    assert b.tokens == 80.0

def test_restore_clamps():
    b = make_bucket(cap=10.0, tokens=9.0)
    b.restore(5.0)
    assert b.tokens == 10.0

def test_inactive_after_quiet_period():
    b = make_bucket(cap=100.0, tokens=100.0)
    b.check_inactive(requests_this_period=2)   # < max(10, 100*0.1)=10
    assert b.active is False

def test_stays_active_when_busy():
    b = make_bucket(cap=100.0, tokens=50.0)
    b.check_inactive(requests_this_period=20)
    assert b.active is True

def test_to_dict_roundtrip():
    b = make_bucket(cap=30.0, tokens=15.0)
    b.last_refill = 1000.0
    d = b.to_dict()
    b2 = TokenBucket.from_dict(d, window_seconds=WINDOW, initial_cap=30.0)
    assert b2.cap == 30.0
    assert b2.tokens == 15.0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /path/to/hermes-router
pytest tests/test_rate_limiter.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'rate_limiter'`

- [ ] **Step 3: Implement `TokenBucket` in `rate_limiter.py`**

```python
# rate_limiter.py
"""
Adaptive multi-window token bucket rate limiter for hermes-router.
"""
from __future__ import annotations
import os
import time
import threading
import json
import logging
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        return default

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default) or default)
    except (TypeError, ValueError):
        return default

RATE_SHORT_WAIT_MS        = _int_env("RATE_SHORT_WAIT_MS", 500)
RATE_HEADROOM_THRESHOLD   = _float_env("RATE_HEADROOM_THRESHOLD", 0.05)
RATE_LEARN_SUCCESS_STREAK = _int_env("RATE_LEARN_SUCCESS_STREAK", 20)
RATE_LEARN_NUDGE_PCT      = _float_env("RATE_LEARN_NUDGE_PCT", 5.0)
RATE_LEARN_CUT_FACTOR     = _float_env("RATE_LEARN_CUT_FACTOR", 0.8)
RATE_LEARN_MAX_MULTIPLIER = _float_env("RATE_LEARN_MAX_MULTIPLIER", 10.0)
RATE_STATE_FLUSH_INTERVAL = _int_env("RATE_STATE_FLUSH_INTERVAL", 60)

# ── Window definitions ────────────────────────────────────────────────────────

WINDOWS = {
    "M":  60.0,
    "H":  3600.0,
    "D":  86400.0,
    "W":  604800.0,
    "Mo": 2592000.0,
}

# Logical limit names → (dimension, window_key)
# dimension: "R" (requests) or "T" (tokens)
LIMIT_KEYS = {
    "RPM": ("R", "M"),  "RPH": ("R", "H"),  "RPD": ("R", "D"),
    "RPW": ("R", "W"),  "RPMo": ("R", "Mo"),
    "TPM": ("T", "M"),  "TPH": ("T", "H"),  "TPD": ("T", "D"),
    "TPW": ("T", "W"),  "TPMo": ("T", "Mo"),
}

# ── TokenBucket ───────────────────────────────────────────────────────────────

class TokenBucket:
    """One window × one dimension (requests or tokens)."""

    def __init__(self, window_seconds: float, cap: float,
                 tokens: float = None, last_refill: float = None):
        self.window_seconds        = window_seconds
        self.cap                   = float(cap)
        self._initial_cap          = float(cap)
        self.tokens                = float(cap if tokens is None else tokens)
        self.last_refill           = last_refill if last_refill is not None else time.time()
        self.active                = True
        self._hit_zero             = False
        self._period_consumed      = 0.0
        self._consecutive_successes = 0

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last_refill)
        rate = self.cap / self.window_seconds if self.window_seconds > 0 else 0.0
        self.tokens = min(self.cap, self.tokens + elapsed * rate)
        self.last_refill = now

    def consume(self, amount: float) -> bool:
        self.refill(time.time())
        if self.tokens >= amount:
            self.tokens -= amount
            self._period_consumed += amount
            if self.tokens <= 0:
                self._hit_zero = True
            return True
        return False

    def restore(self, amount: float) -> None:
        self.tokens = min(self.cap, self.tokens + amount)

    def headroom(self) -> float:
        if self.cap <= 0:
            return 1.0
        self.refill(time.time())
        return max(0.0, min(1.0, self.tokens / self.cap))

    def time_to_refill(self, amount: float) -> float:
        self.refill(time.time())
        if self.tokens >= amount:
            return 0.0
        rate = self.cap / self.window_seconds if self.window_seconds > 0 else 0.0
        if rate <= 0:
            return float("inf")
        return (amount - self.tokens) / rate

    def on_success(self) -> None:
        self._consecutive_successes += 1
        if self._consecutive_successes >= RATE_LEARN_SUCCESS_STREAK:
            ceiling = self._initial_cap * RATE_LEARN_MAX_MULTIPLIER
            if self.cap < ceiling:
                self.cap = min(ceiling, self.cap * (1.0 + RATE_LEARN_NUDGE_PCT / 100.0))
                log.info(f"[rate] nudged cap up to {self.cap:.1f}")
            self._consecutive_successes = 0

    def on_429(self, observed_rate: float) -> None:
        if self._period_consumed >= 3:
            new_cap = max(1.0, observed_rate * RATE_LEARN_CUT_FACTOR)
        else:
            new_cap = max(1.0, self.cap * 0.5)
        log.info(f"[rate] 429 cut cap {self.cap:.1f} → {new_cap:.1f}")
        self.cap = new_cap
        self.tokens = 0.0
        self._consecutive_successes = 0

    def set_from_header(self, cap: float, remaining: float) -> None:
        self.cap    = float(cap)
        self.tokens = float(remaining)
        if not self.active:
            self.active = True
            log.info("[rate] bucket re-activated by header")

    def check_inactive(self, requests_this_period: int) -> None:
        threshold = max(10, self.cap * 0.1)
        if not self._hit_zero and requests_this_period < threshold:
            self.active = False
            log.info(f"[rate] bucket marked inactive (requests={requests_this_period}, threshold={threshold:.0f})")

    def to_dict(self) -> dict:
        return {"cap": self.cap, "tokens": self.tokens, "last_refill": self.last_refill}

    @classmethod
    def from_dict(cls, d: dict, window_seconds: float, initial_cap: float) -> "TokenBucket":
        b = cls(window_seconds=window_seconds, cap=d["cap"],
                tokens=d.get("tokens"), last_refill=d.get("last_refill"))
        b._initial_cap = initial_cap
        return b
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_rate_limiter.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add TokenBucket with adaptive learning loop"
```

---

### Task 2: BucketGroup — per-(key, model) group + default caps

**Files:**
- Modify: `rate_limiter.py` (append)
- Modify: `tests/test_rate_limiter.py` (append)

**Interfaces:**
- Consumes: `TokenBucket` (Task 1)
- Produces:
  - `PROVIDER_RATE_DEFAULTS: dict[str, dict[str, float]]` — built-in cap table
  - `class BucketGroup(provider_name: str, limit_names: list[str] = None)`
    - `buckets: dict[str, TokenBucket]` — keyed by limit name ("RPM", "TPM", …)
    - `_requests_this_period: int` — counter for inactive check
    - `consume(req_count: float, token_count: float) -> tuple[bool, float]` — check all active R-buckets with req_count and all active T-buckets with token_count; return `(all_passed, max_wait_seconds)`; consumes only if all pass
    - `restore_tokens(token_surplus: float) -> None` — call `restore` on all active T-buckets
    - `headroom() -> float` — min headroom across all active buckets; 1.0 if none active
    - `on_success(token_count: float) -> None` — call `on_success()` on all active buckets
    - `on_429(headers: dict) -> None` — call `on_429` on all active buckets; if header data present, call `set_from_header` instead for the relevant bucket
    - `update_from_headers(headers: dict) -> None` — parse `x-ratelimit-*` headers, call `set_from_header` on matching buckets
    - `run_inactive_check() -> None` — call `check_inactive` on all buckets, reset `_requests_this_period`
    - `to_dict() -> dict` — `{limit_name: bucket.to_dict()}` for active buckets only
    - `classmethod from_dict(d: dict, provider_name: str) -> BucketGroup`

- [ ] **Step 1: Write failing tests for BucketGroup**

```python
# append to tests/test_rate_limiter.py
from rate_limiter import BucketGroup, PROVIDER_RATE_DEFAULTS

def test_bucketgroup_consume_passes():
    g = BucketGroup(provider_name="groq")
    ok, wait = g.consume(req_count=1.0, token_count=100.0)
    assert ok is True
    assert wait == 0.0

def test_bucketgroup_consume_fails_when_rpm_empty():
    g = BucketGroup(provider_name="groq")
    rpm = g.buckets.get("RPM")
    if rpm:
        rpm.tokens = 0.0
        ok, wait = g.consume(req_count=1.0, token_count=0.0)
        assert ok is False
        assert wait > 0

def test_bucketgroup_headroom_all_full():
    g = BucketGroup(provider_name="groq")
    assert g.headroom() == pytest.approx(1.0, abs=0.05)

def test_bucketgroup_restore_tokens():
    g = BucketGroup(provider_name="groq")
    tpm = g.buckets.get("TPM")
    if tpm:
        tpm.tokens = 0.0
        g.restore_tokens(100.0)
        assert tpm.tokens == pytest.approx(100.0)

def test_bucketgroup_update_from_headers():
    g = BucketGroup(provider_name="groq")
    g.update_from_headers({
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-remaining-requests": "45",
    })
    rpm = g.buckets.get("RPM")
    assert rpm is not None
    assert rpm.cap == 60.0
    assert rpm.tokens == 45.0

def test_bucketgroup_on_429_cuts_caps():
    g = BucketGroup(provider_name="groq")
    rpm = g.buckets.get("RPM")
    if rpm:
        original_cap = rpm.cap
        rpm._period_consumed = 10.0
        g.on_429({})
        assert rpm.cap < original_cap

def test_bucketgroup_to_dict_roundtrip():
    g = BucketGroup(provider_name="groq")
    d = g.to_dict()
    g2 = BucketGroup.from_dict(d, provider_name="groq")
    assert set(g2.buckets.keys()) == set(d.keys())

def test_default_caps_table_has_groq():
    assert "groq" in PROVIDER_RATE_DEFAULTS
    assert PROVIDER_RATE_DEFAULTS["groq"]["RPM"] == 30

def test_default_caps_fallback():
    assert "_default" in PROVIDER_RATE_DEFAULTS
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_rate_limiter.py::test_bucketgroup_consume_passes -v
```

Expected: `ImportError` or `AttributeError`.

- [ ] **Step 3: Implement `PROVIDER_RATE_DEFAULTS` and `BucketGroup`**

Append to `rate_limiter.py`:

```python
# ── Default caps table ────────────────────────────────────────────────────────
# Only windows with known free-tier defaults are listed. Others start uncapped
# (bucket not created until a header or 429 teaches a real limit).

PROVIDER_RATE_DEFAULTS: dict[str, dict[str, float]] = {
    "groq":         {"RPM": 30,  "TPM": 6_000,  "RPD": 14_400},
    "gemini":       {"RPM": 15,  "TPM": 32_000, "RPD": 1_500},
    "openrouter":   {"RPM": 20,  "TPM": 20_000},
    "mistral":      {"RPM": 5,   "TPM": 16_000},
    "cohere":       {"RPM": 20,  "TPM": 10_000},
    "nvidia":       {"RPM": 40,  "TPM": 40_000},
    "_default":     {"RPM": 10,  "TPM": 10_000},
}


def _load_caps_for(provider_name: str) -> dict[str, float]:
    """Merge built-in defaults < env-var overrides. auth.json overrides are applied
    at BucketGroup construction time by the caller (AdaptiveRateLimiter)."""
    base = dict(PROVIDER_RATE_DEFAULTS.get(provider_name)
                or PROVIDER_RATE_DEFAULTS["_default"])
    # Env overrides: RATE_DEFAULT_GROQ_RPM, RATE_DEFAULT_GROQ_TPM, …
    prefix = f"RATE_DEFAULT_{provider_name.upper()}_"
    for key in list(os.environ):
        if key.startswith(prefix):
            limit_name = key[len(prefix):]  # e.g. "RPM"
            try:
                base[limit_name] = float(os.environ[key])
            except (TypeError, ValueError):
                pass
    return base


# ── BucketGroup ───────────────────────────────────────────────────────────────

# Maps x-ratelimit header suffixes to (dimension, window_key) for bucket lookup.
_HEADER_MAP = {
    "requests":        ("R", "M"),   # x-ratelimit-{limit,remaining}-requests → RPM
    "tokens":          ("T", "M"),   # x-ratelimit-{limit,remaining}-tokens   → TPM
    "requests-day":    ("R", "D"),
    "tokens-day":      ("T", "D"),
    "requests-hour":   ("R", "H"),
    "tokens-hour":     ("T", "H"),
}

def _dim_window_to_limit(dim: str, window: str) -> str:
    prefix = "R" if dim == "R" else "T"
    return f"{prefix}P{window}"   # e.g. "RPM", "TPD"


class BucketGroup:
    """All active token buckets for one (provider, key, model?) scope."""

    def __init__(self, provider_name: str, caps: dict[str, float] = None):
        self.provider_name = provider_name
        self.buckets: dict[str, TokenBucket] = {}
        self._requests_this_period = 0
        caps = caps or _load_caps_for(provider_name)
        for limit_name, cap in caps.items():
            if limit_name not in LIMIT_KEYS:
                continue
            _, window_key = LIMIT_KEYS[limit_name]
            self.buckets[limit_name] = TokenBucket(
                window_seconds=WINDOWS[window_key], cap=float(cap))

    def _active(self):
        return [b for b in self.buckets.values() if b.active]

    def consume(self, req_count: float, token_count: float) -> tuple[bool, float]:
        """Check all active buckets. Consume atomically only if all pass."""
        checks: list[tuple[TokenBucket, float]] = []
        for name, b in self.buckets.items():
            if not b.active:
                continue
            dim, _ = LIMIT_KEYS[name]
            amount = req_count if dim == "R" else token_count
            if amount <= 0:
                continue
            b.refill(time.time())
            if b.tokens < amount:
                wait = b.time_to_refill(amount)
                return False, wait
            checks.append((b, amount))
        for b, amount in checks:
            b.tokens -= amount
            b._period_consumed += amount
            if b.tokens <= 0:
                b._hit_zero = True
        self._requests_this_period += int(req_count)
        return True, 0.0

    def restore_tokens(self, token_surplus: float) -> None:
        for name, b in self.buckets.items():
            if b.active and LIMIT_KEYS.get(name, ("?",))[0] == "T":
                b.restore(token_surplus)

    def headroom(self) -> float:
        active = self._active()
        if not active:
            return 1.0
        return min(b.headroom() for b in active)

    def on_success(self, token_count: float) -> None:
        for b in self._active():
            b.on_success()

    def on_429(self, headers: dict) -> None:
        # If headers contain hard data, use set_from_header for those buckets.
        updated = self._apply_headers(headers, on_429=True)
        for name, b in self.buckets.items():
            if name not in updated and b.active:
                b.on_429(observed_rate=b._period_consumed)
            if not b.active:
                b.active = True   # re-activate on 429
                log.info(f"[rate] bucket {name} re-activated by 429")

    def update_from_headers(self, headers: dict) -> None:
        self._apply_headers(headers, on_429=False)

    def _apply_headers(self, headers: dict, on_429: bool) -> set:
        """Parse x-ratelimit-* headers and update matching buckets. Returns
        set of limit names that were updated from headers."""
        updated = set()
        limits   = {}
        remaining = {}
        for raw_key, val in headers.items():
            k = raw_key.lower()
            if k.startswith("x-ratelimit-limit-"):
                suffix = k[len("x-ratelimit-limit-"):]
                try: limits[suffix] = float(val)
                except (TypeError, ValueError): pass
            elif k.startswith("x-ratelimit-remaining-"):
                suffix = k[len("x-ratelimit-remaining-"):]
                try: remaining[suffix] = float(val)
                except (TypeError, ValueError): pass
        for suffix, cap_val in limits.items():
            rem_val = remaining.get(suffix, cap_val)
            pair = _HEADER_MAP.get(suffix)
            if not pair:
                continue
            dim, window_key = pair
            limit_name = _dim_window_to_limit(dim, window_key)
            if limit_name not in self.buckets:
                _, wk = LIMIT_KEYS[limit_name]
                self.buckets[limit_name] = TokenBucket(
                    window_seconds=WINDOWS[wk], cap=cap_val, tokens=rem_val)
                log.info(f"[rate] created bucket {limit_name} from header cap={cap_val}")
            else:
                self.buckets[limit_name].set_from_header(cap_val, rem_val)
            updated.add(limit_name)
        return updated

    def run_inactive_check(self) -> None:
        n = self._requests_this_period
        for b in self.buckets.values():
            if b.active:
                b.check_inactive(n)
        self._requests_this_period = 0

    def to_dict(self) -> dict:
        return {name: b.to_dict() for name, b in self.buckets.items() if b.active}

    @classmethod
    def from_dict(cls, d: dict, provider_name: str) -> "BucketGroup":
        caps = {name: v["cap"] for name, v in d.items()}
        g = cls(provider_name=provider_name, caps=caps)
        for name, bdict in d.items():
            if name in g.buckets:
                g.buckets[name].tokens = min(bdict["cap"] * 0.5,
                                             bdict.get("tokens", bdict["cap"]))
                g.buckets[name].last_refill = bdict.get("last_refill", time.time())
                g.buckets[name]._initial_cap = bdict["cap"]
        return g
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_rate_limiter.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add BucketGroup with default caps table and header parsing"
```

---

### Task 3: AdaptiveRateLimiter — top-level manager + persistence

**Files:**
- Modify: `rate_limiter.py` (append)
- Modify: `tests/test_rate_limiter.py` (append)

**Interfaces:**
- Consumes: `BucketGroup` (Task 2)
- Produces:
  - `class AdaptiveRateLimiter(state_file: Path)`
    - `get_group(provider_name: str, key: str, model: str | None) -> BucketGroup` — returns (creating if needed) the group for this scope; key is stored truncated to last 8 chars
    - `check_and_consume(provider_name: str, key: str, model: str, req_count: float, token_count: float) -> tuple[bool, float]` — checks provider-wide group AND model group; both must pass; returns `(ok, max_wait_seconds)`. If ok, consumes from both groups.
    - `restore(provider_name: str, key: str, model: str, token_surplus: float) -> None`
    - `on_success(provider_name: str, key: str, model: str, token_count: float) -> None`
    - `on_429(provider_name: str, key: str, model: str, headers: dict) -> None`
    - `update_from_headers(provider_name: str, key: str, model: str, headers: dict) -> None`
    - `headroom(provider_name: str, key: str, model: str) -> float` — min of provider-wide and model-specific headroom
    - `snapshot(provider_name: str, key: str, model: str) -> dict` — returns status dict for `/v1/status`
    - `load() -> None` — read state file, populate groups via `BucketGroup.from_dict`
    - `flush() -> None` — write state file atomically (write to `.tmp`, rename)
    - `start_flush_thread() -> None` — background daemon that flushes every `RATE_STATE_FLUSH_INTERVAL` seconds

- [ ] **Step 1: Write failing tests**

```python
# append to tests/test_rate_limiter.py
import tempfile
from pathlib import Path
from rate_limiter import AdaptiveRateLimiter

def make_limiter(tmp_path):
    return AdaptiveRateLimiter(state_file=Path(tmp_path) / "rate_limits_state.json")

def test_check_and_consume_passes(tmp_path):
    rl = make_limiter(tmp_path)
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 100.0)
    assert ok is True

def test_check_and_consume_fails_when_rpm_full(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    if "RPM" in g.buckets:
        g.buckets["RPM"].tokens = 0.0
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 100.0)
    assert ok is False

def test_headroom_returns_float(tmp_path):
    rl = make_limiter(tmp_path)
    h = rl.headroom("groq", "key-abc12345", "llama")
    assert 0.0 <= h <= 1.0

def test_on_429_updates_buckets(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    if "RPM" in g.buckets:
        original = g.buckets["RPM"].cap
        g.buckets["RPM"]._period_consumed = 10.0
        rl.on_429("groq", "key-abc12345", "llama", {})
        assert g.buckets["RPM"].cap <= original

def test_flush_and_load(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    rl.flush()
    state_file = Path(tmp_path) / "rate_limits_state.json"
    assert state_file.exists()
    rl2 = make_limiter(tmp_path)
    rl2.load()
    h = rl2.headroom("groq", "key-abc12345", "llama")
    assert 0.0 <= h <= 1.0

def test_snapshot_structure(tmp_path):
    rl = make_limiter(tmp_path)
    snap = rl.snapshot("groq", "key-abc12345", "llama")
    assert "provider_wide" in snap
    assert "model" in snap
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_rate_limiter.py::test_check_and_consume_passes -v
```

Expected: `ImportError` or `AttributeError`.

- [ ] **Step 3: Implement `AdaptiveRateLimiter`**

Append to `rate_limiter.py`:

```python
# ── AdaptiveRateLimiter ───────────────────────────────────────────────────────

class AdaptiveRateLimiter:
    """Top-level manager: one BucketGroup per (provider, key[-8:], model?)."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self._lock   = threading.Lock()
        self._groups: dict[str, BucketGroup] = {}   # group_key → BucketGroup

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _group_key(provider_name: str, key: str, model: str | None) -> str:
        key_suffix = (key or "")[-8:] or "unknown"
        if model:
            return f"provider:{provider_name}|key:{key_suffix}|model:{model}"
        return f"provider:{provider_name}|key:{key_suffix}"

    def get_group(self, provider_name: str, key: str, model: str | None) -> BucketGroup:
        gk = self._group_key(provider_name, key, model)
        with self._lock:
            if gk not in self._groups:
                self._groups[gk] = BucketGroup(provider_name=provider_name)
            return self._groups[gk]

    def _both_groups(self, provider_name: str, key: str, model: str):
        """Returns (provider_wide_group, model_group). model_group may equal
        provider_wide_group when model is None."""
        return (self.get_group(provider_name, key, None),
                self.get_group(provider_name, key, model))

    # ── Public API ────────────────────────────────────────────────────────────

    def check_and_consume(self, provider_name: str, key: str, model: str,
                          req_count: float, token_count: float) -> tuple[bool, float]:
        pw, mg = self._both_groups(provider_name, key, model)
        # Check both groups without consuming first.
        pw_ok, pw_wait = pw.consume(req_count, token_count)
        if not pw_ok:
            return False, pw_wait
        mg_ok, mg_wait = mg.consume(req_count, token_count)
        if not mg_ok:
            # Restore provider-wide tokens we already consumed.
            pw.restore_tokens(token_count)
            # Reverse R-bucket consume manually since BucketGroup.consume is
            # optimistic — we must undo the R-bucket debit too.
            pw_rpmb = pw.buckets.get("RPM")
            if pw_rpmb and pw_rpmb.active:
                pw_rpmb.restore(req_count)
            return False, mg_wait
        return True, 0.0

    def restore(self, provider_name: str, key: str, model: str,
                token_surplus: float) -> None:
        if token_surplus <= 0:
            return
        pw, mg = self._both_groups(provider_name, key, model)
        pw.restore_tokens(token_surplus)
        if mg is not pw:
            mg.restore_tokens(token_surplus)

    def on_success(self, provider_name: str, key: str, model: str,
                   token_count: float) -> None:
        pw, mg = self._both_groups(provider_name, key, model)
        pw.on_success(token_count)
        if mg is not pw:
            mg.on_success(token_count)

    def on_429(self, provider_name: str, key: str, model: str,
               headers: dict) -> None:
        pw, mg = self._both_groups(provider_name, key, model)
        pw.on_429(headers)
        if mg is not pw:
            mg.on_429(headers)

    def update_from_headers(self, provider_name: str, key: str, model: str,
                            headers: dict) -> None:
        pw, mg = self._both_groups(provider_name, key, model)
        pw.update_from_headers(headers)
        if mg is not pw:
            mg.update_from_headers(headers)

    def headroom(self, provider_name: str, key: str, model: str) -> float:
        pw, mg = self._both_groups(provider_name, key, model)
        return min(pw.headroom(), mg.headroom())

    def snapshot(self, provider_name: str, key: str, model: str) -> dict:
        """Returns a dict suitable for inclusion in /v1/status."""
        pw = self.get_group(provider_name, key, None)
        mg = self.get_group(provider_name, key, model)

        def _buckets_to_status(g: BucketGroup) -> dict:
            out = {}
            for name, b in g.buckets.items():
                if not b.active:
                    continue
                used = b.cap - b.tokens
                out[name] = {
                    "cap":      round(b.cap, 1),
                    "used":     round(max(0.0, used), 1),
                    "headroom": round(b.headroom(), 3),
                }
            return out

        return {
            "provider_wide": _buckets_to_status(pw),
            "model":         _buckets_to_status(mg),
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            doc = json.loads(self.state_file.read_text())
            if doc.get("version") != 1:
                log.warning("[rate] state file version mismatch, skipping")
                return
            with self._lock:
                for gk, bdict in (doc.get("groups") or {}).items():
                    # Parse provider name from group key for BucketGroup init.
                    pname = gk.split("|")[0].removeprefix("provider:")
                    self._groups[gk] = BucketGroup.from_dict(bdict, provider_name=pname)
            log.info(f"[rate] loaded {len(self._groups)} bucket groups from {self.state_file}")
        except Exception as e:
            log.warning(f"[rate] could not load state file: {e}")

    def flush(self) -> None:
        try:
            with self._lock:
                groups = {gk: g.to_dict() for gk, g in self._groups.items()
                          if g.to_dict()}  # skip empty groups
            doc = {"version": 1, "groups": groups}
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(self.state_file)
            log.debug(f"[rate] flushed {len(groups)} groups to {self.state_file}")
        except Exception as e:
            log.warning(f"[rate] flush failed: {e}")

    def start_flush_thread(self) -> None:
        def _loop():
            while True:
                time.sleep(RATE_STATE_FLUSH_INTERVAL)
                self.flush()
        t = threading.Thread(target=_loop, daemon=True, name="rate-flush")
        t.start()
        log.info(f"[rate] flush thread started (interval={RATE_STATE_FLUSH_INTERVAL}s)")
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_rate_limiter.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add AdaptiveRateLimiter with persistence"
```

---

### Task 4: Integration — import, initialise, load, flush

**Files:**
- Modify: `router.py` (top of file: import + init; near `STATE_FILE` block: init instance; near `_initialize_ratings` call: load; near SIGTERM handler: flush)

**Interfaces:**
- Consumes: `AdaptiveRateLimiter` (Task 3)
- Produces: module-level `rate_limiter: AdaptiveRateLimiter` instance accessible throughout `router.py`

- [ ] **Step 1: Find the right insertion points**

In `router.py`:
- Line ~141: `STATE_FILE = Path(...)` — add `RATE_STATE_FILE` nearby
- Line ~1671: after `pool = CredentialPool(PROVIDERS)` — create instance and load
- Find the SIGTERM/shutdown handler (search for `signal.signal` or `atexit`) — add flush call

```bash
grep -n "signal\|atexit\|SIGTERM\|shutdown\|_save_state\|STATE_FILE" router.py | head -30
```

- [ ] **Step 2: Add `RATE_STATE_FILE` constant and import**

Near line 141 in `router.py`, after `STATE_FILE = ...`:

```python
# (existing)
STATE_FILE = Path(os.environ.get("ROUTER_STATE_FILE", "./router_state.json"))
# (add)
RATE_STATE_FILE = Path(os.environ.get("RATE_STATE_FILE", "./rate_limits_state.json"))
```

At the top of `router.py`, in the stdlib/local imports block, add:

```python
from rate_limiter import AdaptiveRateLimiter
```

- [ ] **Step 3: Create and load the instance**

After line `pool = CredentialPool(PROVIDERS)` (~line 1671):

```python
rate_limiter = AdaptiveRateLimiter(state_file=RATE_STATE_FILE)
rate_limiter.load()
rate_limiter.start_flush_thread()
```

- [ ] **Step 4: Add flush on shutdown**

Find the existing clean-shutdown / state-save block. Add:

```python
rate_limiter.flush()
```

alongside the existing state persistence call so it runs on SIGTERM/SIGINT.

- [ ] **Step 5: Start router and confirm no import errors**

```bash
python router.py &
sleep 3
curl -s http://localhost:8319/ | head -5
kill %1
```

Expected: router starts cleanly, no `ImportError` or `AttributeError` in logs.

- [ ] **Step 6: Commit**

```bash
git add router.py
git commit -m "feat: wire AdaptiveRateLimiter into router startup and shutdown"
```

---

### Task 5: Send-time gate in the try-loop

**Files:**
- Modify: `router.py` — the provider try-loop (~line 4707)

**Interfaces:**
- Consumes: `rate_limiter` instance (Task 4), `RATE_SHORT_WAIT_MS`, `RATE_HEADROOM_THRESHOLD`
- Produces: request gating before each `forward(...)` call; optimistic consume; reconciliation after response

The relevant loop structure (simplified):

```
for candidate in ordered:
    for _ in range(attempts):
        key = pool.get_key(name, model)
        resp = forward(provider, key, payload, streaming, model)
        if resp.status_code == 429: ...
        # success path:
        _add_provider_tokens(name, data, model)
```

- [ ] **Step 1: Locate the exact lines to modify**

```bash
grep -n "key = pool.get_key\|resp = forward\|_add_provider_tokens\|mark_rate_limited" router.py | head -20
```

Note the line numbers for:
- `key = pool.get_key(...)` — gate goes after this
- `resp = forward(...)` — gate must fire before this
- The `resp.status_code == 429` block — add `rate_limiter.on_429(...)` call here
- The success path after `_add_provider_tokens` — add `rate_limiter.on_success(...)` and token reconciliation here

- [ ] **Step 2: Estimate token count before send**

Before `resp = forward(...)`, add:

```python
# Estimate token count for optimistic consume.
# Use the same skip_if_tokens_over value the provider already has configured,
# or fall back to counting message chars / 4 as a rough estimate.
_est_tokens = float(provider.get("skip_if_tokens_over") or 0) or \
    max(1.0, sum(len(str(m.get("content", ""))) for m in
                 payload.get("messages", [])) / 4)
```

- [ ] **Step 3: Add the send-time gate**

After the token estimate, before `resp = forward(...)`:

```python
_rl_ok, _rl_wait = rate_limiter.check_and_consume(
    name, key, model, req_count=1.0, token_count=_est_tokens)
if not _rl_ok:
    _wait_ms = _rl_wait * 1000
    if 0 < _wait_ms <= RATE_SHORT_WAIT_MS:
        log.debug(f"  {name}/{model} thin bucket — waiting {_wait_ms:.0f}ms")
        time.sleep(_rl_wait)
        _rl_ok, _rl_wait = rate_limiter.check_and_consume(
            name, key, model, req_count=1.0, token_count=_est_tokens)
    if not _rl_ok:
        log.info(f"  {name}/{model} rate headroom exhausted ({_rl_wait:.1f}s to refill) — skipping")
        continue
```

- [ ] **Step 4: Add 429 learning hook**

In the `if resp.status_code == 429:` block, after `pool.mark_rate_limited(...)`:

```python
rate_limiter.on_429(name, key, model, dict(resp.headers))
```

- [ ] **Step 5: Add success hook and token reconciliation**

In the non-streaming success path, after `_add_provider_tokens(name, data, model)`:

```python
_actual_tokens = float((data.get("usage") or {}).get("total_tokens") or _est_tokens)
_surplus = _est_tokens - _actual_tokens
rate_limiter.restore(name, key, model, max(0.0, _surplus))
rate_limiter.update_from_headers(name, key, model, dict(resp.headers))
rate_limiter.on_success(name, key, model, _actual_tokens)
```

For the streaming path (`_streaming_with_usage`), on_success and update_from_headers should fire after the stream ends with usage data. Locate the `_streaming_with_usage` function and add the same hooks there, passing `name` and `model` (they're already in scope as closure variables).

- [ ] **Step 6: Smoke test**

```bash
python router.py &
sleep 3
curl -s -X POST http://localhost:8319/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep PROXY_API_KEYS .env | cut -d= -f2)" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}' | python -m json.tool
kill %1
```

Expected: response with choices, no errors in router log.

- [ ] **Step 7: Commit**

```bash
git add router.py
git commit -m "feat: add send-time rate gate and learning hooks in try-loop"
```

---

### Task 6: Headroom-aware routing score in `_get_smart_ordered`

**Files:**
- Modify: `router.py` — `_get_smart_ordered` function (~line 1371)

**Interfaces:**
- Consumes: `rate_limiter` instance (Task 4), `CredentialPool.get_key` (existing)
- Produces: headroom term added to the sort key in `_get_smart_ordered`

The sort key tuple currently ends with `cand["list_index"]`. Add a headroom term just before `list_index` so candidates with more headroom rank higher within a tier.

- [ ] **Step 1: Add headroom lookup to the sort key**

In `_get_smart_ordered`, inside `def _key(cand):`, after the `health` line:

```python
# Rate headroom: 0 = full headroom (best), 1 = empty (worst).
# Peek at the best available key for this (provider, model) without consuming.
_peek_key = pool.get_key(name, model)   # may be None if all cooling
_rate_score = 0.0
if _peek_key:
    _rate_score = 1.0 - rate_limiter.headroom(name, _peek_key, model)
```

Then add `_rate_score` to the return tuple between `health` and `0 if avail else 1`:

```python
return (local_first, tier, price, quality, sort_within, breaker_open, health,
        _rate_score, 0 if avail else 1, fast, cand["list_index"])
```

Note: `pool.get_key()` does **not** consume the key — it only peeks. Verify this is the case before adding the call (it rotates internally but does not debit cooldown state).

- [ ] **Step 2: Verify `pool.get_key` is non-destructive**

```bash
grep -n "def get_key" router.py
```

Read the implementation (~5 lines). If `get_key` rotates the deque, note that this peek will advance the rotation counter once per ordering pass — this is acceptable and matches existing behaviour (the key used for peeking is the same key that would be used for sending).

- [ ] **Step 3: Smoke test with multiple providers configured**

Start router, send 5 requests in quick succession, check that the log shows providers being preferred in headroom order:

```bash
python router.py &
sleep 3
for i in $(seq 5); do
  curl -s -X POST http://localhost:8319/v1/chat/completions \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $(grep PROXY_API_KEYS .env | cut -d= -f2)" \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi '$i'"}]}' \
    -o /dev/null
done
grep "Trying\|headroom" router.log | tail -20
kill %1
```

- [ ] **Step 4: Commit**

```bash
git add router.py
git commit -m "feat: inject headroom score into provider ordering"
```

---

### Task 7: `/v1/status` — expose rate_limits in per-provider stats

**Files:**
- Modify: `router.py` — the `/v1/status` endpoint handler

**Interfaces:**
- Consumes: `rate_limiter.snapshot(...)` (Task 3), existing per-provider stats structure
- Produces: `rate_limits` key added to each provider entry in the `/v1/status` response

- [ ] **Step 1: Find the status endpoint**

```bash
grep -n "v1/status\|/status" router.py | head -10
```

Locate the function that builds the per-provider dict (likely iterates `PROVIDERS` and calls `stats.health_bucket` etc.).

- [ ] **Step 2: Add rate_limits to each provider entry**

Inside the per-provider loop, after the existing stats fields, add:

```python
# Rate limits for the first active key on this provider.
_first_key = pool.get_key(p["name"], p.get("model") or (p.get("models") or [""])[0])
_models = p.get("models") or [p.get("model", "")]
_rl_per_model = {}
for _m in _models:
    _k = pool.get_key(p["name"], _m) or _first_key
    if _k:
        _rl_per_model[_m] = rate_limiter.snapshot(p["name"], _k, _m)
provider_entry["rate_limits"] = _rl_per_model
```

- [ ] **Step 3: Verify JSON output**

```bash
python router.py &
sleep 3
curl -s http://localhost:8319/v1/status | python -m json.tool | grep -A 10 "rate_limits"
kill %1
```

Expected: `rate_limits` key with nested model entries each containing `provider_wide` and `model` sub-objects.

- [ ] **Step 4: Commit**

```bash
git add router.py
git commit -m "feat: expose rate_limits in /v1/status per-provider stats"
```

---

### Task 8: Dashboard — Rate headroom column

**Files:**
- Modify: `router.py` — the dashboard HTML/JS embedded in the `/dashboard` route

**Interfaces:**
- Consumes: `rate_limits` field in `/v1/status` response (Task 7)
- Produces: "Rate headroom" coloured progress bar column in the existing per-provider/model table

The dashboard is rendered by an embedded HTML template in `router.py`. The existing RPM progress bar in the access-keys table uses this pattern (around line 4336–4354):

```javascript
const rpmPct = rpmMax ? Math.min(rpmUsed / rpmMax * 100, 100) : 0;
const rpmColor = rpmPct > 80 ? 'red' : rpmPct > 50 ? 'yellow' : 'green';
```

- [ ] **Step 1: Locate the provider/model status table in the dashboard JS**

```bash
grep -n "provider_wide\|rate_limits\|headroom\|prog-track\|prog-fill" router.py | head -20
```

Find where per-provider model rows are rendered in JS (likely a `renderProviders` or similar function).

- [ ] **Step 2: Add headroom bar to provider model rows**

In the JS that renders each provider/model row, after existing status indicators, add:

```javascript
// Rate headroom bar
const rlData = (prov.rate_limits || {})[model] || {};
const pwHeadroom = rlData.provider_wide ? Math.min(...Object.values(rlData.provider_wide).map(b => b.headroom)) : 1.0;
const mHeadroom  = rlData.model        ? Math.min(...Object.values(rlData.model).map(b => b.headroom))        : 1.0;
const headroom   = Math.min(pwHeadroom, mHeadroom);
const hPct       = Math.round(headroom * 100);
const hColor     = hPct >= 50 ? 'green' : hPct >= 20 ? 'yellow' : 'red';
const hTitle     = Object.entries({...rlData.provider_wide, ...rlData.model})
  .map(([k, v]) => `${k}: ${v.used}/${v.cap}`).join(' | ') || 'no data';
const headroomBar = `<div title="${hTitle}" class="prog-track" style="width:60px">
  <div class="prog-fill ${hColor}" style="width:${hPct}%"></div></div>
  <span class="muted" style="font-size:10px">${hPct}%</span>`;
```

Inject `headroomBar` into the row HTML alongside the existing status badges.

- [ ] **Step 3: Verify dashboard renders**

```bash
python router.py &
sleep 3
curl -s http://localhost:8319/dashboard | grep -c "prog-fill"
kill %1
```

Expected: count ≥ 1 (at least one progress bar rendered).

- [ ] **Step 4: Commit**

```bash
git add router.py
git commit -m "feat: add rate headroom bar to dashboard provider/model rows"
```

---

### Task 9: Inactive bucket check — background sweep

**Files:**
- Modify: `rate_limiter.py` — add `run_all_inactive_checks` method to `AdaptiveRateLimiter`
- Modify: `router.py` — wire into the background flush thread or add a separate sweep thread

**Interfaces:**
- Consumes: `BucketGroup.run_inactive_check` (Task 2)
- Produces: periodic inactive-bucket pruning

- [ ] **Step 1: Add sweep method to `AdaptiveRateLimiter`**

In `rate_limiter.py`, inside `AdaptiveRateLimiter`:

```python
def run_all_inactive_checks(self) -> None:
    """Call run_inactive_check on every group. Called periodically."""
    with self._lock:
        groups = list(self._groups.values())
    for g in groups:
        g.run_inactive_check()
    log.debug(f"[rate] inactive check complete ({len(groups)} groups)")
```

- [ ] **Step 2: Wire into the flush thread**

In `start_flush_thread`, update `_loop` to also run the inactive check on a longer cadence (once per flush × 10 = every ~10 minutes by default):

```python
def _loop():
    sweep_counter = 0
    while True:
        time.sleep(RATE_STATE_FLUSH_INTERVAL)
        self.flush()
        sweep_counter += 1
        if sweep_counter >= 10:
            self.run_all_inactive_checks()
            sweep_counter = 0
```

- [ ] **Step 3: Add test**

```python
# append to tests/test_rate_limiter.py
def test_inactive_check_runs_without_error(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    rl.run_all_inactive_checks()  # should not raise
```

- [ ] **Step 4: Run all tests**

```bash
pytest tests/test_rate_limiter.py -v
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add periodic inactive bucket sweep"
```

---

### Task 10: `.env.example` and documentation update

**Files:**
- Modify: `.env.example`
- Modify: `documentation/configuration.md`
- Modify: `website/src/content/docs/configuration.md`

**Interfaces:**
- Produces: documented env vars matching spec §10; no code changes

- [ ] **Step 1: Add env vars to `.env.example`**

Find the existing rate-limit section (~line 131) in `.env.example` and append after it:

```bash
# ── Adaptive upstream rate limiter ────────────────────────────────────────────
# hermes-router learns each provider's rate limits from response headers and 429
# signals, persisting the learned state to rate_limits_state.json.
#
# RATE_STATE_FILE=./rate_limits_state.json   # path to persist learned limits
# RATE_SHORT_WAIT_MS=500          # max ms to sleep when a bucket is nearly empty
# RATE_HEADROOM_THRESHOLD=0.05    # fraction of cap below which bucket is "thin"
# RATE_LEARN_SUCCESS_STREAK=20    # requests before nudging a cap up
# RATE_LEARN_NUDGE_PCT=5          # % to increase cap on a success streak
# RATE_LEARN_CUT_FACTOR=0.8       # multiplier applied to observed rate on 429
# RATE_LEARN_MAX_MULTIPLIER=10    # cap ceiling as multiple of initial default
# RATE_STATE_FLUSH_INTERVAL=60    # seconds between background state flushes
#
# Per-provider cap overrides (override built-in defaults):
# RATE_DEFAULT_GROQ_RPM=60
# RATE_DEFAULT_GROQ_TPM=10000
```

- [ ] **Step 2: Add a section to `documentation/configuration.md`**

Find the "Per-key budgets & rate limits" section header and add a new section after it:

```markdown
### Adaptive upstream rate limiter

hermes-router automatically discovers and tracks each upstream provider's rate limits.
It starts from conservative built-in defaults and adjusts caps up or down based on
`x-ratelimit-*` response headers and 429 signals. Learned limits persist across
restarts in `rate_limits_state.json`.

| Env var | Default | Description |
|---|---|---|
| `RATE_STATE_FILE` | `./rate_limits_state.json` | Path to learned-limits state file |
| `RATE_SHORT_WAIT_MS` | `500` | Max ms to sleep when a bucket is nearly empty before failing over |
| `RATE_HEADROOM_THRESHOLD` | `0.05` | Fraction of cap below which a bucket triggers pacing |
| `RATE_LEARN_SUCCESS_STREAK` | `20` | Consecutive successes before nudging a cap up |
| `RATE_LEARN_NUDGE_PCT` | `5` | Percent to increase cap on a success streak |
| `RATE_LEARN_CUT_FACTOR` | `0.8` | Multiplier applied to observed rate on 429 |
| `RATE_LEARN_MAX_MULTIPLIER` | `10` | Cap ceiling as multiple of the initial default |
| `RATE_STATE_FLUSH_INTERVAL` | `60` | Seconds between background state flushes |
| `RATE_DEFAULT_<PROVIDER>_<WINDOW>` | — | Override built-in default cap (e.g. `RATE_DEFAULT_GROQ_RPM=60`) |

The rate limit state is visible in the dashboard (Rate headroom column) and in
`/v1/status` under each provider's `rate_limits` key.
```

- [ ] **Step 3: Mirror the same section to `website/src/content/docs/configuration.md`**

The website docs mirror `documentation/configuration.md`. Apply the identical addition.

- [ ] **Step 4: Commit**

```bash
git add .env.example documentation/configuration.md website/src/content/docs/configuration.md
git commit -m "docs: document adaptive rate limiter env vars and configuration"
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Task(s) |
|---|---|
| §3 Bucket granularity (key × model + provider-wide) | Task 1, 2, 3 |
| §4 TokenBucket mechanics (refill, consume, reconcile) | Task 1 |
| §5.1 Header-driven learning | Task 2, 5 |
| §5.2 Signal-driven learning (429 cut, success nudge) | Task 1, 5 |
| §5.3 Inactive bucket heuristic | Task 2, 9 |
| §6 Default caps table + env/auth.json overrides | Task 2 |
| §7.1 Headroom score in routing | Task 6 |
| §7.2 Send-time gate + short wait | Task 5 |
| §7.3 Token reconciliation | Task 5 |
| §8 Persistence (flush, load, conservative restore) | Task 3, 4 |
| §9.1 Dashboard headroom bar | Task 8 |
| §9.2 `/v1/status` rate_limits | Task 7 |
| §9.3 Logging levels | Tasks 1–5 (log calls included in implementations) |
| §10 Configuration reference | Task 10 |
| auth.json `rate_defaults` override | Not yet covered — add to Task 2 |

**Gap identified:** `auth.json` `rate_defaults` override (spec §6 override precedence item 2) has no task. Adding it to Task 2's scope: in `_load_caps_for`, after the env-var scan, the caller (`AdaptiveRateLimiter`) should merge any `rate_defaults` from `auth.json`. This is a small addition to Task 2's `BucketGroup` constructor — load auth.json at construction time if a path is available, merge provider-specific overrides. The implementation in Task 2's Step 3 should be updated: pass an optional `auth_caps: dict` param to `BucketGroup.__init__` and merge it highest-priority into the caps dict.

**Type consistency check:** `check_and_consume` → `(bool, float)` — used correctly in Task 5. `snapshot` returns `dict` with `provider_wide` + `model` keys — consumed correctly in Tasks 7 and 8. `headroom` returns `float` — used correctly in Task 6.

**Placeholder scan:** None found.
