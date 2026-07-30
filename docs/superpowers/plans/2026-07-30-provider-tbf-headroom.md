# Provider vs Model TBF Headroom Divergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make provider-wide and model TBF headroom diverge on the dashboard by initializing provider-wide caps at 10× model defaults, while keeping full dual debit and adding a bounded surprise soft cut when a healthy-looking model 429s.

**Architecture:** Keep dual AND-gated `BucketGroup`s. New provider-wide groups get `base_cap * RATE_PROVIDER_CAP_MULTIPLIER` (default 10). Model groups unchanged. On 429, pass pre-attempt model headroom from the router into `AdaptiveRateLimiter.on_429`; if ≥ 0.9 and the 60s surprise throttle allows, apply one extra soft cut on provider-wide after the normal soft cut. No probe traffic. Caps on both scopes continue to grow via existing success nudges after natural refill.

**Tech Stack:** Python 3, `rate_limiter.py`, Flask `router.py`, pytest, markdown docs.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-30-provider-tbf-headroom-design.md`
- Full dual debit / reconcile unchanged (same absolute spend on pw and model)
- Headers never mutate provider-wide; Retry-After stays model-only
- No synthetic probe / half-open requests
- ×10 applies only when **creating** a new provider-wide group — never re-multiply on `load()`
- Surprise threshold: model headroom **before the attempt** ≥ `0.9` (must use pre-`check_and_consume` value; post-consume headroom is already reduced)
- Surprise bound: at most one surprise soft cut per provider-wide group per rolling 60s wall clock
- Both model and provider caps must remain able to grow via existing success-streak nudges

---

## File Structure

| File | Responsibility |
|---|---|
| `rate_limiter.py` | `RATE_PROVIDER_CAP_MULTIPLIER`; ×10 caps for new PW groups; surprise throttle + extra soft cut in `on_429` |
| `router.py` | Pass pre-attempt `_current_headroom` into `on_429` (chat + embeddings + transient-200 paths) |
| `tests/test_rate_limiter.py` | ×10 init, divergent headroom, surprise / no-surprise, load no re-multiply, growth |
| `documentation/configuration.md` | Document ×10 prior + surprise heuristic |
| `website/src/content/docs/configuration.md` | Mirror |
| `website/src/content/docs/architecture.md` | One-line note if TBF paragraph exists |
| `.env.example` | Document `RATE_PROVIDER_CAP_MULTIPLIER` if other `RATE_*` knobs are listed there |

---

### Task 1: ×10 provider-wide caps on new groups

**Files:**
- Modify: `rate_limiter.py` (module constant; `_caps_for` / `_get_group_unlocked`)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: existing `_load_caps_for`, `AdaptiveRateLimiter._caps_for`, `_get_group_unlocked`
- Produces:
  - `RATE_PROVIDER_CAP_MULTIPLIER: float` (env `RATE_PROVIDER_CAP_MULTIPLIER`, default `10.0`)
  - `_caps_for(self, provider_name: str, *, provider_wide: bool = False) -> dict[str, float]`
    — when `provider_wide=True`, multiply each cap by `RATE_PROVIDER_CAP_MULTIPLIER`
  - `_get_group_unlocked` passes `provider_wide=(model is None)` into `_caps_for` **only when creating** a missing group

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rate_limiter.py`:

```python
def test_new_provider_wide_caps_are_10x_model_defaults(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "nvidia/nemotron")
    assert "TPM" in pw.buckets and "TPM" in mg.buckets
    assert pw.buckets["TPM"].cap == pytest.approx(mg.buckets["TPM"].cap * 10.0)
    assert pw.buckets["RPM"].cap == pytest.approx(mg.buckets["RPM"].cap * 10.0)


def test_single_consume_model_headroom_drops_more_than_provider(tmp_path):
    rl = make_limiter(tmp_path)
    # Force creation of both groups at full tokens
    rl.get_group("openrouter", "key-abc12345", None)
    rl.get_group("openrouter", "key-abc12345", "m")
    ok, _ = rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 2000.0)
    assert ok is True
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    assert mg.headroom() < pw.headroom()
    # Same absolute TPM debit; PW cap is 10× so remaining fraction is higher
    assert pw.buckets["TPM"].cap == pytest.approx(mg.buckets["TPM"].cap * 10.0)


def test_load_does_not_remultiply_provider_caps(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    # Simulate a learned (non-×10) cap already persisted
    pw.buckets["TPM"].cap = 12345.0
    pw.buckets["TPM"].tokens = 12345.0
    rl.flush()
    rl2 = make_limiter(tmp_path)
    rl2.load()
    pw2 = rl2.get_group("openrouter", "key-abc12345", None)
    assert pw2.buckets["TPM"].cap == pytest.approx(12345.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_new_provider_wide_caps_are_10x_model_defaults \
  tests/test_rate_limiter.py::test_single_consume_model_headroom_drops_more_than_provider \
  tests/test_rate_limiter.py::test_load_does_not_remultiply_provider_caps -v
```

Expected: FAIL (PW and model caps still equal on create).

- [ ] **Step 3: Implement ×10 caps for new provider-wide groups**

In `rate_limiter.py`:

1. Add near other `RATE_*` env knobs:

```python
RATE_PROVIDER_CAP_MULTIPLIER = _float_env("RATE_PROVIDER_CAP_MULTIPLIER", 10.0)
```

2. Change `_caps_for` to accept `provider_wide` and scale:

```python
def _caps_for(self, provider_name: str, *, provider_wide: bool = False) -> dict[str, float]:
    caps = _load_caps_for(provider_name)
    overrides = self._auth_rate_defaults.get(provider_name, {})
    if overrides:
        caps = {**caps, **overrides}
    if provider_wide:
        mult = RATE_PROVIDER_CAP_MULTIPLIER
        caps = {k: float(v) * mult for k, v in caps.items()}
    return caps
```

3. In `_get_group_unlocked`, when creating a new group:

```python
def _get_group_unlocked(self, provider_name: str, key: str,
                        model: str | None) -> BucketGroup:
    gk = self._group_key(provider_name, key, model)
    if gk not in self._groups:
        self._groups[gk] = BucketGroup(
            provider_name=provider_name,
            caps=self._caps_for(provider_name, provider_wide=(model is None)),
        )
    return self._groups[gk]
```

Do **not** change `load()` / `BucketGroup.from_dict` — persisted caps stay as stored.

- [ ] **Step 4: Run tests to verify they pass**

Run the same three pytest selectors as Step 2.

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: init provider-wide TBF caps at 10x model defaults"
```

---

### Task 2: Surprise soft cut on high-headroom model 429

**Files:**
- Modify: `rate_limiter.py` (`AdaptiveRateLimiter.on_429`; per-group surprise timestamp)
- Modify: `router.py` (pass `model_headroom_before=` into all `rate_limiter.on_429` call sites that have a pre-attempt peek)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: existing asymmetric `on_429` (soft PW / hard model)
- Produces:
  - `AdaptiveRateLimiter.on_429(self, provider_name, key, model, headers, *, model_headroom_before: float | None = None) -> None`
  - Internal: `_surprise_cut_at: dict[str, float]` keyed by provider-wide group id, or attribute on `BucketGroup` e.g. `_last_surprise_cut_at: float`
  - Constant: surprise threshold `0.9`; window `60.0` seconds
  - When `model is not None` and `model_headroom_before is not None` and `model_headroom_before >= 0.9` and throttle allows: after normal soft `pw.on_429(...)`, call `pw.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)` once more (second soft cut), then stamp surprise time

- [ ] **Step 1: Write the failing tests**

```python
def test_on_429_surprise_extra_soft_cut_when_model_headroom_high(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 10.0)
    pw = rl.get_group("groq", "key-abc12345", None)
    # Restore PW tokens/caps to a known full state for a clean cut measurement
    for b in pw.buckets.values():
        b.cap = 100.0
        b.tokens = 100.0
        b._period_consumed = 0.0
    cap_before = pw.buckets["RPM"].cap
    # High pre-attempt model headroom → surprise path
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=1.0)
    # Normal soft (no history) → ×0.9, then surprise soft → ×0.9 again
    assert pw.buckets["RPM"].cap == pytest.approx(cap_before * 0.9 * 0.9)


def test_on_429_no_surprise_when_model_headroom_low(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 10.0)
    pw = rl.get_group("groq", "key-abc12345", None)
    for b in pw.buckets.values():
        b.cap = 100.0
        b.tokens = 100.0
        b._period_consumed = 0.0
    cap_before = pw.buckets["RPM"].cap
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=0.2)
    assert pw.buckets["RPM"].cap == pytest.approx(cap_before * 0.9)


def test_on_429_surprise_throttled_within_60s(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 10.0)
    pw = rl.get_group("groq", "key-abc12345", None)
    for b in pw.buckets.values():
        b.cap = 100.0
        b.tokens = 100.0
        b._period_consumed = 0.0
    t0 = 1_000_000.0
    monkeypatch.setattr(rate_limiter.time, "time", lambda: t0)
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=1.0)
    cap_after_first = pw.buckets["RPM"].cap
    for b in pw.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 0.0
    monkeypatch.setattr(rate_limiter.time, "time", lambda: t0 + 10.0)
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=1.0)
    # Second 429 within 60s: soft once only (no second surprise)
    assert pw.buckets["RPM"].cap == pytest.approx(cap_after_first * 0.9)
```

Add `import rate_limiter` at top of test file if not already imported as module for `monkeypatch.setattr(rate_limiter.time, ...)`.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest \
  tests/test_rate_limiter.py::test_on_429_surprise_extra_soft_cut_when_model_headroom_high \
  tests/test_rate_limiter.py::test_on_429_no_surprise_when_model_headroom_low \
  tests/test_rate_limiter.py::test_on_429_surprise_throttled_within_60s -v
```

Expected: FAIL (`on_429` does not accept / ignore `model_headroom_before`).

- [ ] **Step 3: Implement surprise path in `rate_limiter.py`**

1. On `BucketGroup.__init__`, add `self._last_surprise_cut_at = 0.0`.
2. Extend `AdaptiveRateLimiter.on_429`:

```python
def on_429(self, provider_name: str, key: str, model: str,
           headers: dict, *, model_headroom_before: float | None = None) -> None:
    with self._lock:
        pw, mg = self._both_groups_unlocked(provider_name, key, model)
        if mg is pw:
            pw.on_429(headers, apply_retry_after=True, apply_headers=True, soft=False)
            return
        pw.on_429(headers, apply_retry_after=False, apply_headers=False, soft=True)
        mg.on_429(headers, apply_retry_after=True, apply_headers=True, soft=False)
        now = time.time()
        if (model_headroom_before is not None
                and model_headroom_before >= 0.9
                and (now - pw._last_surprise_cut_at) >= 60.0):
            pw.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)
            pw._last_surprise_cut_at = now
```

Do not persist `_last_surprise_cut_at` in `to_dict` (in-memory throttle only is fine).

- [ ] **Step 4: Wire router call sites**

In `router.py`, every `rate_limiter.on_429(...)` that follows a successful pre-attempt headroom read must pass that value:

Chat loop (already has `_current_headroom` before consume):

```python
rate_limiter.on_429(
    name, key, model, dict(resp.headers),
    model_headroom_before=_current_headroom,
)
```

Embeddings path (has `_current_headroom`):

```python
rate_limiter.on_429(
    name, key, em, dict(resp.headers),
    model_headroom_before=_current_headroom,
)
```

Transient HTTP-200 rate-limit path inside the success branch: that path may not have a fresh local name — use the same `_current_headroom` from the enclosing attempt loop (it is still in scope for that key attempt). Pass it the same way. If a call site has no pre-attempt peek, omit the kwarg (defaults to `None` → no surprise).

- [ ] **Step 5: Run tests to verify they pass**

Run the three surprise tests plus a quick regression:

```bash
python -m pytest tests/test_rate_limiter.py::test_on_429_asymmetric_cuts \
  tests/test_rate_limiter.py::test_on_429_surprise_extra_soft_cut_when_model_headroom_high \
  tests/test_rate_limiter.py::test_on_429_no_surprise_when_model_headroom_low \
  tests/test_rate_limiter.py::test_on_429_surprise_throttled_within_60s -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add rate_limiter.py router.py tests/test_rate_limiter.py
git commit -m "feat: surprise soft-cut provider TBF when high-headroom model 429s"
```

---

### Task 3: Assert caps can still grow (model + provider)

**Files:**
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: existing `on_success` / nudge behavior
- Produces: regression tests only (no production API change)

- [ ] **Step 1: Write the failing tests** (or assert against current behavior — should already pass once Task 1 exists; write first if missing)

```python
def test_model_and_provider_caps_grow_via_success_nudges(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 1.0)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    pw_cap0 = pw.buckets["TPM"].cap
    mg_cap0 = mg.buckets["TPM"].cap
    # Provider streak default 10; model streak default 20
    for _ in range(20):
        rl.on_success("openrouter", "key-abc12345", "m", 1.0)
    assert pw.buckets["TPM"].cap > pw_cap0
    assert mg.buckets["TPM"].cap > mg_cap0
```

- [ ] **Step 2: Run test**

```bash
python -m pytest tests/test_rate_limiter.py::test_model_and_provider_caps_grow_via_success_nudges -v
```

Expected: PASS with current nudge wiring (if FAIL, fix `on_success` wiring — do not add probes).

- [ ] **Step 3: Commit**

```bash
git add tests/test_rate_limiter.py
git commit -m "test: assert model and provider TBF caps grow via success nudges"
```

---

### Task 4: Documentation

**Files:**
- Modify: `documentation/configuration.md` (adaptive rate limiter section)
- Modify: `website/src/content/docs/configuration.md` (mirror)
- Modify: `website/src/content/docs/architecture.md` (one sentence if TBF is mentioned)
- Modify: `.env.example` only if it already documents `RATE_LEARN_*` knobs

**Interfaces:**
- Docs only

- [ ] **Step 1: Update configuration docs**

Extend the existing two-scope blurb to include:

- Provider-wide starts at **10×** the model/base default caps for that provider (`RATE_PROVIDER_CAP_MULTIPLIER`, default `10`) when a provider-wide group is first created.
- Same absolute debit on both scopes; percentages diverge because caps differ.
- Surprise: if model headroom was ≥ 90% before an attempt that 429s, provider-wide takes one extra soft cut (max once per 60s per provider-wide group).
- Both scopes can grow caps via success nudges after natural refill; no probe traffic.
- Persisted provider caps are not re-multiplied on load.

Add a table row:

| `RATE_PROVIDER_CAP_MULTIPLIER` | `10` | Multiplier applied to base caps when creating a new provider-wide TBF group |

Mirror the same text in `website/src/content/docs/configuration.md`.

- [ ] **Step 2: Architecture one-liner**

If `website/src/content/docs/architecture.md` (and `documentation/architecture.md` if present) mentions TBF scopes, add that provider-wide uses a ×10 prior vs model defaults so shared-ceiling % does not twin the model bar.

- [ ] **Step 3: Commit**

```bash
git add documentation/configuration.md website/src/content/docs/configuration.md \
  website/src/content/docs/architecture.md documentation/architecture.md .env.example
git commit -m "docs: document provider TBF ×10 prior and surprise soft cut"
```

(Only add files that actually changed.)

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Provider-wide init at ×10 base caps | Task 1 |
| Full dual debit unchanged; % diverge | Task 1 |
| Load does not re-×10 persisted caps | Task 1 |
| Surprise soft cut when model headroom ≥ 0.9 | Task 2 |
| Surprise ≤1 / 60s per PW group | Task 2 |
| Router passes pre-attempt headroom | Task 2 |
| No probes | Global / Task 3 (growth via nudges only) |
| Model + provider caps can grow | Task 3 |
| Docs | Task 4 |

## Placeholder / consistency review

- No TBD/TODO left in steps.
- `on_429(..., model_headroom_before=)` signature is consistent across Task 2 tests and router wiring.
- `_caps_for(..., provider_wide=)` only used at **create** time in `_get_group_unlocked`.
