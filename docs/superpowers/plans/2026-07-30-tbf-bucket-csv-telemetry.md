# TBF Bucket CSV Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opt-in append-only CSV of TBF cap/headroom at each learning event for Calc-based convergence analysis.

**Architecture:** Add a small `BucketEventCsv` writer in `rate_limiter.py`, gated by `RATE_BUCKET_CSV_ENABLED`. TokenBucket learn methods return a `CapChange` when `cap` actually changes; `BucketGroup` / `AdaptiveRateLimiter` attach scope identity (`provider`, `key_hint`, `model`, `scope`) and call `record()`. Disabled = no file I/O.

**Tech Stack:** Python 3 stdlib (`csv`, `threading`, `pathlib`, `datetime`), existing `rate_limiter.py` + `pytest`.

**Base branch:** Implement on a branch from `main` (this feature needs `rate_limiter.py`; do not land it only on `pr/exclude-models`).

## Global Constraints

- Emit rows only when a bucket’s `cap` actually changes (or a new header-created bucket); no periodic snapshots.
- CSV header: `datetime,provider,key_hint,model,scope,bucket,event,reason,cap,old_cap,tokens,used,headroom`
- `datetime` = ISO-8601 local time with offset (first column).
- `RATE_BUCKET_CSV_ENABLED` on for `1`/`true`/`yes` (case-insensitive); path `RATE_BUCKET_CSV` default `./rate_bucket_events.csv`.
- Always append across restarts; write header only when file missing or size 0.
- Soft-fail all CSV I/O (warn, never raise into learn/request path).
- Never write full API keys — `key_hint` is last-8 suffix only.
- No dashboard / Prometheus / token-caps CSV changes.
- YAGNI: keep helper in `rate_limiter.py` (no new package).

## File map

| File | Role |
|---|---|
| `rate_limiter.py` | `CapChange`, `BucketEventCsv`, env flags, wire-up at learn sites |
| `tests/test_rate_limiter.py` | Unit + wiring tests (extend existing file) |
| `documentation/configuration.md` | Env var rows + one-line purpose |
| `website/src/content/docs/configuration.md` | Same env rows (keep in sync) |
| `.gitignore` | Ignore `rate_bucket_events.csv` (runtime artifact) |

---

### Task 1: BucketEventCsv writer

**Files:**
- Modify: `rate_limiter.py` (add helper near top, after config env readers)
- Test: `tests/test_rate_limiter.py`

**Interfaces:**
- Produces:
  - `CSV_COLUMNS: list[str]` — fixed header order
  - `class CapChange`: `old_cap: float`, `event: str`, `reason: str`
  - `class BucketEventCsv` with:
    - `__init__(self, enabled: bool, path: Path)`
    - `record(self, *, provider: str, key_hint: str, model: str, scope: str, bucket: str, event: str, reason: str, cap: float, old_cap: float, tokens: float, used: float, headroom: float) -> None`
    - `@staticmethod from_env() -> BucketEventCsv`
  - Module singleton `_bucket_csv: BucketEventCsv` created via `from_env()` at import (tests may replace it)
  - `_truthy_env(name: str) -> bool` — true for `1`/`true`/`yes` case-insensitive
  - `_row_metrics(bucket: TokenBucket) -> tuple[float, float, float]` → `(tokens, used, headroom)` after mutation (`used = max(0, cap - tokens)`; headroom via `bucket.headroom()`)

- [ ] **Step 1: Write the failing tests for the CSV writer**

Add to `tests/test_rate_limiter.py`:

```python
import csv
from pathlib import Path
from rate_limiter import BucketEventCsv, CSV_COLUMNS, _truthy_env

def test_truthy_env(monkeypatch):
    monkeypatch.setenv("RATE_BUCKET_CSV_ENABLED", "YES")
    assert _truthy_env("RATE_BUCKET_CSV_ENABLED") is True
    monkeypatch.setenv("RATE_BUCKET_CSV_ENABLED", "0")
    assert _truthy_env("RATE_BUCKET_CSV_ENABLED") is False

def test_csv_disabled_writes_nothing(tmp_path):
    path = tmp_path / "events.csv"
    w = BucketEventCsv(enabled=False, path=path)
    w.record(provider="groq", key_hint="deadbeef", model="llama",
             scope="model", bucket="RPM", event="nudge", reason="success_streak",
             cap=31.5, old_cap=30.0, tokens=10.0, used=21.5, headroom=10 / 31.5)
    assert not path.exists()

def test_csv_writes_header_once_then_appends(tmp_path):
    path = tmp_path / "events.csv"
    w = BucketEventCsv(enabled=True, path=path)
    kwargs = dict(provider="groq", key_hint="deadbeef", model="llama",
                  scope="model", bucket="RPM", event="nudge", reason="success_streak",
                  cap=31.5, old_cap=30.0, tokens=10.0, used=21.5, headroom=10 / 31.5)
    w.record(**kwargs)
    w.record(**{**kwargs, "cap": 33.0, "old_cap": 31.5})
    text = path.read_text()
    assert text.count(",".join(CSV_COLUMNS)) == 1
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 2
    assert list(rows[0].keys()) == CSV_COLUMNS
    assert rows[0]["datetime"]  # non-empty ISO local
    assert rows[0]["provider"] == "groq"
    assert rows[0]["bucket"] == "RPM"
    assert float(rows[0]["old_cap"]) == 30.0
    assert float(rows[1]["cap"]) == 33.0

def test_csv_soft_fails_on_unwritable(tmp_path, monkeypatch):
    path = tmp_path / "nope" / "events.csv"
    w = BucketEventCsv(enabled=True, path=path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom, raising=True)
    # Must not raise
    w.record(provider="groq", key_hint="deadbeef", model="",
             scope="provider_wide", bucket="TPM", event="cut", reason="soft_429",
             cap=100.0, old_cap=200.0, tokens=0.0, used=100.0, headroom=0.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rate_limiter.py::test_truthy_env tests/test_rate_limiter.py::test_csv_disabled_writes_nothing tests/test_rate_limiter.py::test_csv_writes_header_once_then_appends tests/test_rate_limiter.py::test_csv_soft_fails_on_unwritable -v`

Expected: FAIL with `ImportError` / `BucketEventCsv` not defined.

- [ ] **Step 3: Implement `BucketEventCsv` and helpers**

In `rate_limiter.py`, after existing env helpers, add:

```python
import csv
from dataclasses import dataclass
from datetime import datetime

def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")

CSV_COLUMNS = [
    "datetime", "provider", "key_hint", "model", "scope", "bucket",
    "event", "reason", "cap", "old_cap", "tokens", "used", "headroom",
]

@dataclass
class CapChange:
    old_cap: float
    event: str
    reason: str

class BucketEventCsv:
    def __init__(self, enabled: bool, path: Path):
        self.enabled = enabled
        self.path = Path(path)
        self._lock = threading.Lock()
        self._header_ready = False
        self._last_warn_at = 0.0

    @staticmethod
    def from_env() -> "BucketEventCsv":
        return BucketEventCsv(
            enabled=_truthy_env("RATE_BUCKET_CSV_ENABLED"),
            path=Path(os.environ.get("RATE_BUCKET_CSV") or "./rate_bucket_events.csv"),
        )

    def record(self, *, provider: str, key_hint: str, model: str, scope: str,
               bucket: str, event: str, reason: str,
               cap: float, old_cap: float, tokens: float,
               used: float, headroom: float) -> None:
        if not self.enabled:
            return
        row = {
            "datetime": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "provider": provider,
            "key_hint": key_hint,
            "model": model or "",
            "scope": scope,
            "bucket": bucket,
            "event": event,
            "reason": reason,
            "cap": cap,
            "old_cap": old_cap,
            "tokens": tokens,
            "used": used,
            "headroom": headroom,
        }
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new_file = (not self.path.exists()) or self.path.stat().st_size == 0
                with self.path.open("a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    if new_file or not self._header_ready:
                        if new_file:
                            w.writeheader()
                        self._header_ready = True
                    w.writerow(row)
            except OSError as e:
                now = time.time()
                if now - self._last_warn_at >= 60.0:
                    log.warning(f"[rate] bucket CSV write failed: {e}")
                    self._last_warn_at = now

def _row_metrics(b: "TokenBucket") -> tuple[float, float, float]:
    tokens = float(b.tokens)
    cap = float(b.cap)
    used = max(0.0, cap - tokens)
    headroom = b.headroom()
    return tokens, used, headroom

_bucket_csv = BucketEventCsv.from_env()
```

Fix the soft-fail test if `Path.open` monkeypatch is too broad: prefer injecting a path under a file-as-directory, or patch `BucketEventCsv.record`’s open via a subclass. Preferred durable approach in implementation:

```python
def test_csv_soft_fails_on_unwritable(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not-a-dir")
    path = blocker / "events.csv"  # parent is a file → mkdir/open fails
    w = BucketEventCsv(enabled=True, path=path)
    w.record(provider="groq", key_hint="deadbeef", model="",
             scope="provider_wide", bucket="TPM", event="cut", reason="soft_429",
             cap=100.0, old_cap=200.0, tokens=0.0, used=100.0, headroom=0.0)
```

Use that version instead of monkeypatching `Path.open`.

- [ ] **Step 4: Run writer tests to verify they pass**

Run: `pytest tests/test_rate_limiter.py::test_truthy_env tests/test_rate_limiter.py::test_csv_disabled_writes_nothing tests/test_rate_limiter.py::test_csv_writes_header_once_then_appends tests/test_rate_limiter.py::test_csv_soft_fails_on_unwritable -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "$(cat <<'EOF'
feat: add BucketEventCsv writer for TBF cap telemetry

EOF
)"
```

---

### Task 2: Return CapChange from learn mutations and record with scope identity

**Files:**
- Modify: `rate_limiter.py` (`TokenBucket`, `BucketGroup`, `AdaptiveRateLimiter`)
- Test: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `BucketEventCsv.record`, `CapChange`, `_bucket_csv`, `_row_metrics`
- Produces (updated signatures):
  - `TokenBucket.on_success(...) -> CapChange | None`
  - `TokenBucket.on_429(...) -> CapChange | None` — reason `hard_429` | `soft_429` | `soft_floor`
  - `TokenBucket.ensure_fits(...) -> CapChange | None`
  - `TokenBucket.set_from_header(...) -> CapChange | None` — `None` if stale **or** same-cap remaining refresh; `CapChange` if new pin with cap change. **Breaking note:** callers today check truthiness of `bool`; `CapChange` is truthy, `None` is falsy — update `_apply_headers` accordingly (treat as updated iff return is not None).
  - `BucketGroup` methods accept optional identity kwargs OR AdaptiveRateLimiter records after comparing — **required approach below**.

**Required wire-up approach (do this, not a vague alternative):**

1. Change `TokenBucket` mutators to return `CapChange | None` as above.
2. Add helper on `AdaptiveRateLimiter`:

```python
def _emit(self, provider_name: str, key: str, model: str | None,
          bucket_name: str, b: TokenBucket, change: CapChange | None) -> None:
    if change is None:
        return
    tokens, used, headroom = _row_metrics(b)
    _bucket_csv.record(
        provider=provider_name,
        key_hint=(key or "")[-8:] or "unknown",
        model=model or "",
        scope="provider_wide" if model is None else "model",
        bucket=bucket_name,
        event=change.event,
        reason=change.reason,
        cap=b.cap,
        old_cap=change.old_cap,
        tokens=tokens,
        used=used,
        headroom=headroom,
    )
```

3. `BucketGroup.on_success` / `on_429` / `_apply_headers` / `consume` (around `ensure_fits`) should return or collect `list[tuple[str, CapChange]]` for changed buckets **or** accept an `emit` callback. Prefer returning a list of `(name, CapChange)` and let `AdaptiveRateLimiter` call `_emit` (group does not know key_hint).

Concrete `TokenBucket` bodies:

```python
def on_success(self, streak: int | None = None, nudge_pct: float | None = None) -> CapChange | None:
    if self._header_pinned:
        return None
    need = RATE_LEARN_SUCCESS_STREAK if streak is None else streak
    pct = RATE_LEARN_NUDGE_PCT if nudge_pct is None else nudge_pct
    self._consecutive_successes += 1
    if self._consecutive_successes < need:
        return None
    old = self.cap
    self.cap = self.cap * (1.0 + pct / 100.0)
    log.info(f"[rate] nudged cap up to {self.cap:.1f}")
    self._consecutive_successes = 0
    return CapChange(old_cap=old, event="nudge", reason="success_streak")

def on_429(self, observed_rate: float, *, soft: bool = False) -> CapChange | None:
    old = self.cap
    if self._period_consumed >= 3:
        factor = RATE_LEARN_CUT_FACTOR_PROVIDER if soft else RATE_LEARN_CUT_FACTOR
        new_cap = max(1.0, observed_rate * factor)
    else:
        frac = RATE_LEARN_SOFT_CUT_FACTOR if soft else 0.5
        new_cap = max(1.0, self.cap * frac)
    reason = "soft_429" if soft else "hard_429"
    if soft:
        floor = max(1.0, getattr(self, "_floor_cap", self._initial_cap)
                    * RATE_LEARN_SOFT_FLOOR_FRAC)
        if new_cap < floor:
            log.info(f"[rate] soft cut floored {new_cap:.1f} → {floor:.1f}")
            new_cap = floor
            reason = "soft_floor"
    log.info(f"[rate] 429 {'soft ' if soft else ''}cut cap {self.cap:.1f} → {new_cap:.1f}")
    self.cap = new_cap
    self.tokens = 0.0
    self._consecutive_successes = 0
    self._period_consumed = 0.0
    self._header_pinned = False
    if new_cap == old:
        return None
    return CapChange(old_cap=old, event="cut", reason=reason)

def ensure_fits(self, amount: float) -> CapChange | None:
    burst = max(1.0, RATE_REQUEST_BURST_FACTOR)
    target = float(amount) * burst
    if target <= self.cap:
        return None
    old = self.cap
    self.cap = target
    self.tokens = max(self.tokens, self.cap)
    self._floor_cap = max(getattr(self, "_floor_cap", old), float(amount))
    log.info(f"[rate] lifted cap {old:.1f} → {self.cap:.1f} "
             f"(request {amount:.0f} × burst {burst:g})")
    return CapChange(old_cap=old, event="lift", reason="request_burst")

def set_from_header(self, cap: float, remaining: float,
                    observed_at: float | None = None) -> CapChange | None:
    obs = time.time() if observed_at is None else float(observed_at)
    if obs < self._header_obs_at:
        return None
    old = self.cap
    new_cap = float(cap)
    self.cap = new_cap
    self.tokens = float(remaining)
    self._header_pinned = True
    self._header_obs_at = obs
    self._consecutive_successes = 0
    if not self.active:
        self.active = True
        log.info("[rate] bucket re-activated by header")
    if new_cap == old:
        return None  # remaining-only refresh: no CSV row
    return CapChange(old_cap=old, event="header_pin", reason="header")
```

For **new** buckets created in `_apply_headers`, after `TokenBucket(...)` + `set_from_header`, if `set_from_header` returns `None` because constructor already set the same cap, still emit once: treat creation as a change with `old_cap=new_cap` **or** call `set_from_header` on an empty bucket constructed with a sentinel. Simplest: construct with `cap=new_cap`, then if `set_from_header` returns `None`, emit `CapChange(old_cap=new_cap, event="header_pin", reason="header")` only when the bucket was just inserted into `self.buckets` (new key). Spec: “newly created” may share cap with constructor — **emit one `header_pin` row for newly inserted buckets even when `old_cap == cap`.**

```python
# inside _apply_headers, new-bucket branch:
b = TokenBucket(window_seconds=WINDOWS[wk], cap=cap_val, tokens=rem_val)
change = b.set_from_header(cap_val, rem_val, observed_at=obs)
if change is None:
    change = CapChange(old_cap=cap_val, event="header_pin", reason="header")
self.buckets[limit_name] = b
updated.add(limit_name)
changes.append((limit_name, change))
```

Propagate `changes` lists up through `BucketGroup.on_success` / `on_429` / `update_from_headers` / `consume`, and have `AdaptiveRateLimiter` emit for both provider-wide and model groups with the correct `model` argument (`None` for PW).

Update existing tests that assumed `set_from_header` returns `True`/`False`:

```python
assert b.set_from_header(...) is not None   # was True
assert b.set_from_header(...) is None       # was False
```

- [ ] **Step 1: Write failing integration tests for emit paths**

```python
import rate_limiter

def _enable_csv(monkeypatch, tmp_path):
    path = tmp_path / "tbf.csv"
    monkeypatch.setenv("RATE_BUCKET_CSV_ENABLED", "1")
    monkeypatch.setenv("RATE_BUCKET_CSV", str(path))
    rate_limiter._bucket_csv = rate_limiter.BucketEventCsv.from_env()
    return path

def test_csv_nudge_emits_row(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    # Force short streak via monkeypatch on module constants if needed
    monkeypatch.setattr(rate_limiter, "RATE_LEARN_SUCCESS_STREAK", 2)
    monkeypatch.setattr(rate_limiter, "RATE_LEARN_SUCCESS_STREAK_PROVIDER", 2)
    key, model = "sk-testkey99", "llama-3"
    rl.check_and_consume("groq", key, model, 1.0, 10.0)
    rl.on_success("groq", key, model, 10.0)
    rl.on_success("groq", key, model, 10.0)
    rows = list(csv.DictReader(path.open()))
    assert any(r["event"] == "nudge" and r["bucket"] in ("RPM", "TPM", "RPD")
               for r in rows)
    assert all(r["key_hint"] == key[-8:] for r in rows)

def test_csv_hard_429_emits_cut(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    key, model = "sk-testkey99", "llama-3"
    rl.check_and_consume("groq", key, model, 1.0, 10.0)
    # Build period history so cut uses observed_rate path
    g = rl.get_group("groq", key, model)
    for b in g.buckets.values():
        b._period_consumed = 10.0
    rl.on_429("groq", key, model, {})
    rows = list(csv.DictReader(path.open()))
    assert any(r["event"] == "cut" and r["reason"] == "hard_429" for r in rows)

def test_csv_ensure_fits_lift(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    key, model = "sk-testkey99", "llama-3"
    ok, _ = rl.check_and_consume("mistral", key, model, 1.0, 71_621.0)
    assert ok is True
    rows = list(csv.DictReader(path.open()))
    assert any(r["event"] == "lift" and r["reason"] == "request_burst"
               and r["bucket"] == "TPM" for r in rows)

def test_csv_header_pin_skips_same_cap_refresh(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    key, model = "sk-testkey99", "llama-3"
    headers = {
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-remaining-requests": "20",
        "x-ratelimit-limit-tokens": "6000",
        "x-ratelimit-remaining-tokens": "5000",
    }
    rl.update_from_headers("groq", key, model, headers, observed_at=100.0)
    n1 = len(list(csv.DictReader(path.open()))) if path.exists() else 0
    # Same caps, different remaining
    headers2 = {
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-remaining-requests": "10",
        "x-ratelimit-limit-tokens": "6000",
        "x-ratelimit-remaining-tokens": "1000",
    }
    rl.update_from_headers("groq", key, model, headers2, observed_at=200.0)
    n2 = len(list(csv.DictReader(path.open())))
    assert n2 == n1  # no new rows for remaining-only refresh

def test_csv_disabled_no_file_on_learn(monkeypatch, tmp_path):
    path = tmp_path / "should_not_exist.csv"
    monkeypatch.delenv("RATE_BUCKET_CSV_ENABLED", raising=False)
    monkeypatch.setenv("RATE_BUCKET_CSV", str(path))
    rate_limiter._bucket_csv = rate_limiter.BucketEventCsv.from_env()
    rl = make_limiter(tmp_path)
    rl.on_429("groq", "sk-testkey99", "llama-3", {})
    assert not path.exists()
```

Also update existing `test_set_from_header` / `test_older_observed_at_ignored` / `test_newer_observed_at_applies` / `test_header_pin_blocks_on_success_nudge` for `CapChange | None` returns.

- [ ] **Step 2: Run new tests — expect fail**

Run: `pytest tests/test_rate_limiter.py::test_csv_nudge_emits_row tests/test_rate_limiter.py::test_csv_hard_429_emits_cut tests/test_rate_limiter.py::test_csv_ensure_fits_lift tests/test_rate_limiter.py::test_csv_header_pin_skips_same_cap_refresh tests/test_rate_limiter.py::test_csv_disabled_no_file_on_learn -v`

Expected: FAIL (no emit wiring yet / return types still bool).

- [ ] **Step 3: Implement CapChange returns + AdaptiveRateLimiter `_emit` wiring**

Implement the `TokenBucket` changes, propagate change lists through `BucketGroup`, call `_emit` from `AdaptiveRateLimiter.on_success`, `on_429`, `update_from_headers`, and `check_and_consume` (for lifts inside `consume`). Keep learn behavior (caps, tokens, pins) identical aside from return types.

- [ ] **Step 4: Run full rate_limiter suite**

Run: `pytest tests/test_rate_limiter.py -v`

Expected: PASS (all green, including pre-existing tests updated for `CapChange`).

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "$(cat <<'EOF'
feat: emit TBF cap-change rows to optional CSV telemetry

EOF
)"
```

---

### Task 3: Docs and gitignore

**Files:**
- Modify: `documentation/configuration.md` (adaptive rate limiter env table)
- Modify: `website/src/content/docs/configuration.md` (same table)
- Modify: `.gitignore`

**Interfaces:** none (docs only)

- [ ] **Step 1: Add env rows and one sentence**

In both configuration docs, in the adaptive upstream rate limiter table, add:

| `RATE_BUCKET_CSV_ENABLED` | off | When `1`/`true`/`yes`, append TBF cap-change events to a CSV |
| `RATE_BUCKET_CSV` | `./rate_bucket_events.csv` | Path for that append-only event log (Calc/Excel-friendly) |

After the table (or in the paragraph that mentions dashboard visibility), add one sentence:

> For local development, enable `RATE_BUCKET_CSV_ENABLED` to append each cap change (nudge/cut/header pin/lift) with headroom into a CSV you can open in LibreOffice Calc or Excel; the file always appends across restarts.

- [ ] **Step 2: Ignore runtime CSV**

Add to `.gitignore`:

```gitignore
# Opt-in TBF learning telemetry (RATE_BUCKET_CSV_ENABLED)
rate_bucket_events.csv
```

- [ ] **Step 3: Commit**

```bash
git add documentation/configuration.md website/src/content/docs/configuration.md .gitignore
git commit -m "$(cat <<'EOF'
docs: document RATE_BUCKET_CSV telemetry env vars

EOF
)"
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|---|---|
| Event-driven cap+headroom CSV | Task 2 |
| Columns + datetime first | Task 1 (`CSV_COLUMNS`) |
| Opt-in flag + optional path | Task 1 (`from_env`) |
| Append across restarts; header if empty | Task 1 |
| soft_floor / hard_429 / soft_429 / nudge / header_pin / lift | Task 2 |
| Header remaining-only = no row; new bucket = row | Task 2 |
| Soft-fail I/O | Task 1 |
| Unit tests | Tasks 1–2 |
| Config docs | Task 3 |
| No dashboard/Prometheus/token-caps | Honored (no tasks) |

No TBD/placeholder steps remain. `set_from_header` return type change is called out with test updates.
