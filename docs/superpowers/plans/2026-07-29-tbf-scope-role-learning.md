# TBF Scope-Role Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make provider-wide and model TBF scopes diverge via asymmetric learning (headers → model only; soft PW 429 cuts; faster PW nudges) and expose an explicit `role` for ops clarity.

**Architecture:** Keep dual AND-gated `BucketGroup`s. Model groups remain authoritative (headers + hard 429 + Retry-After). Provider-wide groups become shared-ceiling estimates (consume-only inventory, soft 429, faster success nudges, no header overwrite). Dashboard/API label `role: estimate | authoritative`.

**Tech Stack:** Python 3, `rate_limiter.py`, Flask dashboard in `router.py`, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-29-tbf-scope-role-learning-design.md`
- AND-gate consume + model-only Retry-After stay unchanged
- Headers never mutate provider-wide buckets
- Soft PW defaults: `RATE_LEARN_CUT_FACTOR_PROVIDER=0.95`, `RATE_LEARN_SOFT_CUT_FACTOR=0.9`
- Fast PW nudge defaults: `RATE_LEARN_SUCCESS_STREAK_PROVIDER=10`, `RATE_LEARN_NUDGE_PCT_PROVIDER=8`
- Model hard-cut / nudge knobs unchanged (`CUT_FACTOR=0.8`, streak `20`, nudge `5%`, low-history `×0.5`)
- Do not parse 429 bodies for account-vs-model attribution
- Do not change `/v1/status` snapshot shape (role only on `list_groups`)

---

## File Structure

| File | Responsibility |
|---|---|
| `rate_limiter.py` | Env knobs; `TokenBucket` soft cut + parameterized nudge; `BucketGroup` / `AdaptiveRateLimiter` asymmetric wiring; `role` on `list_groups` |
| `tests/test_rate_limiter.py` | Unit tests for soft cut, faster PW nudge, headers model-only, role |
| `router.py` | Dashboard role cue + panel blurb (inline HTML/JS) |
| `.env.example` | Document new `RATE_LEARN_*` knobs |
| `documentation/configuration.md` | Document roles + env vars |
| `website/src/content/docs/configuration.md` | Mirror configuration docs |
| `website/src/content/docs/architecture.md` | One-sentence TBF role note |
| `documentation/architecture.md` | Mirror if present |

---

### Task 1: TokenBucket soft cut + parameterized nudge

**Files:**
- Modify: `rate_limiter.py` (module env constants; `TokenBucket.on_429`, `TokenBucket.on_success`)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: existing `TokenBucket`
- Produces:
  - Module constants: `RATE_LEARN_CUT_FACTOR_PROVIDER`, `RATE_LEARN_SOFT_CUT_FACTOR`, `RATE_LEARN_SUCCESS_STREAK_PROVIDER`, `RATE_LEARN_NUDGE_PCT_PROVIDER`
  - `TokenBucket.on_429(self, observed_rate: float, *, soft: bool = False) -> None`
  - `TokenBucket.on_success(self, streak: int | None = None, nudge_pct: float | None = None) -> None`
    — `None` means use model defaults (`RATE_LEARN_SUCCESS_STREAK` / `RATE_LEARN_NUDGE_PCT`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rate_limiter.py`:

```python
def test_on_429_soft_with_history():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0, soft=True)
    # Default RATE_LEARN_CUT_FACTOR_PROVIDER = 0.95
    assert b.cap == pytest.approx(9.5)
    assert b.tokens == 0.0


def test_on_429_soft_without_history():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 1.0
    b.on_429(observed_rate=1.0, soft=True)
    # Default RATE_LEARN_SOFT_CUT_FACTOR = 0.9
    assert b.cap == pytest.approx(54.0)


def test_on_429_hard_unchanged_default():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0)  # soft=False default
    assert b.cap == pytest.approx(8.0)


def test_on_success_custom_streak_and_nudge():
    b = make_bucket(cap=10.0, tokens=10.0)
    for _ in range(10):
        b.on_success(streak=10, nudge_pct=8.0)
    assert b.cap == pytest.approx(10.0 * 1.08)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rate_limiter.py::test_on_429_soft_with_history tests/test_rate_limiter.py::test_on_429_soft_without_history tests/test_rate_limiter.py::test_on_success_custom_streak_and_nudge -v`

Expected: FAIL (`soft` / unexpected kwargs, or wrong caps)

- [ ] **Step 3: Implement env knobs + TokenBucket methods**

In `rate_limiter.py`, after existing `RATE_LEARN_*` constants:

```python
RATE_LEARN_CUT_FACTOR_PROVIDER      = _float_env("RATE_LEARN_CUT_FACTOR_PROVIDER", 0.95)
RATE_LEARN_SOFT_CUT_FACTOR          = _float_env("RATE_LEARN_SOFT_CUT_FACTOR", 0.9)
RATE_LEARN_SUCCESS_STREAK_PROVIDER  = _int_env("RATE_LEARN_SUCCESS_STREAK_PROVIDER", 10)
RATE_LEARN_NUDGE_PCT_PROVIDER       = _float_env("RATE_LEARN_NUDGE_PCT_PROVIDER", 8.0)
```

Replace `TokenBucket.on_success` and `on_429`:

```python
def on_success(self, streak: int | None = None, nudge_pct: float | None = None) -> None:
    need = RATE_LEARN_SUCCESS_STREAK if streak is None else streak
    pct = RATE_LEARN_NUDGE_PCT if nudge_pct is None else nudge_pct
    self._consecutive_successes += 1
    if self._consecutive_successes >= need:
        self.cap = self.cap * (1.0 + pct / 100.0)
        log.info(f"[rate] nudged cap up to {self.cap:.1f}")
        self._consecutive_successes = 0

def on_429(self, observed_rate: float, *, soft: bool = False) -> None:
    if self._period_consumed >= 3:
        factor = RATE_LEARN_CUT_FACTOR_PROVIDER if soft else RATE_LEARN_CUT_FACTOR
        new_cap = max(1.0, observed_rate * factor)
    else:
        frac = RATE_LEARN_SOFT_CUT_FACTOR if soft else 0.5
        new_cap = max(1.0, self.cap * frac)
    log.info(f"[rate] 429 {'soft ' if soft else ''}cut cap {self.cap:.1f} → {new_cap:.1f}")
    self.cap = new_cap
    self.tokens = 0.0
    self._consecutive_successes = 0
    self._period_consumed = 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rate_limiter.py::test_on_429_soft_with_history tests/test_rate_limiter.py::test_on_429_soft_without_history tests/test_rate_limiter.py::test_on_429_hard_unchanged_default tests/test_rate_limiter.py::test_on_success_custom_streak_and_nudge tests/test_rate_limiter.py::test_on_429_with_history_cuts_cap tests/test_rate_limiter.py::test_on_429_without_history_halves tests/test_rate_limiter.py::test_on_success_nudge tests/test_rate_limiter.py::test_on_success_nudge_past_former_ceiling -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add soft 429 cut and parameterized TBF nudges"
```

---

### Task 2: Asymmetric AdaptiveRateLimiter learning

**Files:**
- Modify: `rate_limiter.py` (`BucketGroup.on_429`, `BucketGroup.on_success`, `AdaptiveRateLimiter.update_from_headers`, `AdaptiveRateLimiter.on_429`, `AdaptiveRateLimiter.on_success`)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: Task 1 `TokenBucket.on_429(..., soft=)`, `on_success(streak=, nudge_pct=)`
- Produces:
  - `BucketGroup.on_429(self, headers: dict, apply_retry_after: bool = True, *, apply_headers: bool = True, soft: bool = False) -> None`
  - `BucketGroup.on_success(self, token_count: float, streak: int | None = None, nudge_pct: float | None = None) -> None`
  - `AdaptiveRateLimiter.update_from_headers` → model group only
  - `AdaptiveRateLimiter.on_429` → PW soft + no headers + no Retry-After; model hard + headers + Retry-After
  - `AdaptiveRateLimiter.on_success` → PW uses provider streak/nudge; model uses defaults

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rate_limiter.py`:

```python
def test_update_from_headers_model_only(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    pw_rpm_before = pw.buckets["RPM"].cap
    pw_tok_before = pw.buckets["RPM"].tokens
    rl.update_from_headers("groq", "key-abc12345", "llama", {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "40",
    })
    assert mg.buckets["RPM"].cap == pytest.approx(100.0)
    assert mg.buckets["RPM"].tokens == pytest.approx(40.0)
    assert pw.buckets["RPM"].cap == pytest.approx(pw_rpm_before)
    assert pw.buckets["RPM"].tokens == pytest.approx(pw_tok_before)


def test_on_429_asymmetric_cuts(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    for g in (pw, mg):
        g.buckets["RPM"].cap = 100.0
        g.buckets["RPM"].tokens = 50.0
        g.buckets["RPM"]._period_consumed = 10.0
    rl.on_429("groq", "key-abc12345", "llama", {})
    assert mg.buckets["RPM"].cap == pytest.approx(8.0)    # 10 * 0.8
    assert pw.buckets["RPM"].cap == pytest.approx(9.5)    # 10 * 0.95
    assert pw.blocked_until <= time.time()


def test_on_429_headers_apply_to_model_not_pw(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    pw_cap = pw.buckets["RPM"].cap
    for g in (pw, mg):
        g.buckets["RPM"]._period_consumed = 1.0  # low history → fractional cut path if used
    rl.on_429("groq", "key-abc12345", "llama", {
        "x-ratelimit-limit-requests": "200",
        "x-ratelimit-remaining-requests": "0",
        "Retry-After": "30",
    })
    assert mg.buckets["RPM"].cap == pytest.approx(200.0)
    assert mg.buckets["RPM"].tokens == pytest.approx(0.0)
    # PW must not take header caps; soft-cut instead
    assert pw.buckets["RPM"].cap != pytest.approx(200.0)
    assert pw.buckets["RPM"].cap == pytest.approx(pw_cap * 0.9)


def test_on_success_pw_nudges_faster(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    pw.buckets["RPM"].cap = 100.0
    mg.buckets["RPM"].cap = 100.0
    for _ in range(10):
        rl.on_success("groq", "key-abc12345", "llama", 1.0)
    assert pw.buckets["RPM"].cap == pytest.approx(100.0 * 1.08)
    assert mg.buckets["RPM"].cap == pytest.approx(100.0)  # needs 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rate_limiter.py::test_update_from_headers_model_only tests/test_rate_limiter.py::test_on_429_asymmetric_cuts tests/test_rate_limiter.py::test_on_429_headers_apply_to_model_not_pw tests/test_rate_limiter.py::test_on_success_pw_nudges_faster -v`

Expected: FAIL (headers still update PW; equal cuts; equal nudges)

- [ ] **Step 3: Implement BucketGroup + AdaptiveRateLimiter wiring**

Update `BucketGroup.on_success`:

```python
def on_success(self, token_count: float,
               streak: int | None = None, nudge_pct: float | None = None) -> None:
    for b in self._active():
        b.on_success(streak=streak, nudge_pct=nudge_pct)
```

Update `BucketGroup.on_429` signature and body start:

```python
def on_429(self, headers: dict, apply_retry_after: bool = True, *,
           apply_headers: bool = True, soft: bool = False) -> None:
    updated = self._apply_headers(headers, on_429=True) if apply_headers else set()
    for name, b in self.buckets.items():
        if name in updated:
            continue
        if not b.active:
            b.active = True
            log.info(f"[rate] bucket {name} re-activated by 429")
        b.on_429(observed_rate=b._period_consumed, soft=soft)
    # ... existing Retry-After block unchanged ...
```

Update `AdaptiveRateLimiter` methods:

```python
def on_success(self, provider_name: str, key: str, model: str,
               token_count: float) -> None:
    with self._lock:
        pw, mg = self._both_groups_unlocked(provider_name, key, model)
        pw.on_success(
            token_count,
            streak=RATE_LEARN_SUCCESS_STREAK_PROVIDER,
            nudge_pct=RATE_LEARN_NUDGE_PCT_PROVIDER,
        )
        if mg is not pw:
            mg.on_success(token_count)  # model defaults
        else:
            # model is None path: already nudged with PW knobs only once
            pass

def on_429(self, provider_name: str, key: str, model: str,
           headers: dict) -> None:
    with self._lock:
        pw, mg = self._both_groups_unlocked(provider_name, key, model)
        if mg is pw:
            pw.on_429(headers, apply_retry_after=True, apply_headers=True, soft=False)
        else:
            pw.on_429(headers, apply_retry_after=False, apply_headers=False, soft=True)
            mg.on_429(headers, apply_retry_after=True, apply_headers=True, soft=False)

def update_from_headers(self, provider_name: str, key: str, model: str,
                        headers: dict) -> None:
    with self._lock:
        _pw, mg = self._both_groups_unlocked(provider_name, key, model)
        mg.update_from_headers(headers)
```

Note: when `model` is such that `mg is pw` (only if caller passed `model=None` into `_both_groups` — current public API always passes a model string, so `mg` is always the model group). Keep the `mg is pw` branch for safety matching today’s code.

- [ ] **Step 4: Run new + related regression tests**

Run: `pytest tests/test_rate_limiter.py -v`

Expected: PASS (including sibling Retry-After, rollback, load migration)

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: differentiate provider-wide vs model TBF learning"
```

---

### Task 3: `role` on list_groups + dashboard labels

**Files:**
- Modify: `rate_limiter.py` (`list_groups`)
- Modify: `tests/test_rate_limiter.py`
- Modify: `router.py` (panel blurb ~4068, `renderRateLimits` scope cell ~5152, `renderRateDetail` meta ~5200)

**Interfaces:**
- Consumes: existing `list_groups` row shape
- Produces: each group dict includes `"role": "authoritative" | "estimate"`

- [ ] **Step 1: Write the failing test**

```python
def test_list_groups_includes_role(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    rows = rl.list_groups(include_orphans=True, configured_ids={pw, mg})
    by_id = {r["id"]: r for r in rows}
    assert by_id[pw]["role"] == "estimate"
    assert by_id[mg]["role"] == "authoritative"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rate_limiter.py::test_list_groups_includes_role -v`

Expected: FAIL (`KeyError: 'role'` or assert)

- [ ] **Step 3: Add `role` in `list_groups`**

In the `out.append({...})` dict in `list_groups`, add:

```python
"role": "authoritative" if parsed["model"] else "estimate",
```

(next to existing `"scope": "model" if parsed["model"] else "provider_wide"`)

- [ ] **Step 4: Update dashboard copy in `router.py`**

Panel intro (~line 4068):

```html
<div class="page-intro" style="padding:12px 14px 0">
  Live adaptive rate-limit buckets. Model rows are authoritative (headers + hard 429).
  Provider-wide rows are shared-ceiling estimates (no header sync; softer cuts; faster recovery).
  Click a row for per-bucket detail. Clear drops learned caps for that scope.
</div>
```

In `renderRateLimits`, change scope cell:

```javascript
const scopeLabel = g.scope === 'model' ? (g.model || '—') : 'provider-wide';
const role = g.role === 'estimate'
  ? ' <span class="pill pill-grey">estimate</span>'
  : (g.role === 'authoritative' ? ' <span class="pill pill-ok">authoritative</span>' : '');
// ...
`<td class="mono muted">${scopeLabel}${role}</td>`
```

In `renderRateDetail` meta:

```javascript
const roleNote = g.role === 'estimate'
  ? ' · shared-ceiling estimate — no header sync'
  : (g.role === 'authoritative' ? ' · authoritative' : '');
document.getElementById('rl-detail-meta').innerHTML =
  `${cfg} · ${g.scope === 'model' ? 'model ' + (g.model || '') : 'provider-wide'}${roleNote} · <span class="mono">${g.id}</span>`;
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_rate_limiter.py::test_list_groups_includes_role tests/test_rate_limiter.py::test_list_groups_filters_orphans -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py router.py
git commit -m "feat: expose TBF scope roles in API and dashboard"
```

---

### Task 4: Documentation

**Files:**
- Modify: `.env.example`
- Modify: `documentation/configuration.md`
- Modify: `website/src/content/docs/configuration.md`
- Modify: `website/src/content/docs/architecture.md`
- Modify: `documentation/architecture.md` (if the file exists; skip if absent)

**Interfaces:**
- Consumes: knobs and roles from Tasks 1–3
- Produces: documented env vars + role semantics for operators

- [ ] **Step 1: Update `.env.example`**

After the existing `RATE_LEARN_CUT_FACTOR` comment block, add:

```bash
# RATE_LEARN_CUT_FACTOR_PROVIDER=0.95  # soft PW cut vs observed rate on 429
# RATE_LEARN_SOFT_CUT_FACTOR=0.9       # soft PW cut vs current cap (low history)
# RATE_LEARN_SUCCESS_STREAK_PROVIDER=10  # faster PW nudge streak
# RATE_LEARN_NUDGE_PCT_PROVIDER=8        # faster PW nudge percent
```

Add a one-line note above them:

```bash
# Provider-wide buckets are shared-ceiling estimates (no header sync).
# Model buckets are authoritative (headers + hard 429 + Retry-After).
```

- [ ] **Step 2: Update configuration docs (both copies)**

In `documentation/configuration.md` and `website/src/content/docs/configuration.md`, under Adaptive upstream rate limiter:

1. After the intro paragraph, add:

> Two scopes are tracked per key: **model** groups are authoritative (learn from `x-ratelimit-*` headers and hard 429 cuts; `Retry-After` holds that model only). **Provider-wide** groups are a shared-ceiling estimate (debited by all models on the key; softer 429 cuts; faster success recovery; never overwritten by response headers).

2. Extend the env table with the four new knobs (defaults as in Global Constraints).

- [ ] **Step 3: Update architecture docs**

In the TBF / multiple-models paragraph of `website/src/content/docs/architecture.md` (and `documentation/architecture.md` if present), after the sentence about per-(key, model) buckets plus provider-wide, add:

> Provider-wide buckets are a shared-ceiling **estimate** (no header sync; softer cuts; faster recovery). Model buckets remain authoritative for that model’s upstream limits.

- [ ] **Step 4: Commit**

```bash
git add .env.example documentation/configuration.md website/src/content/docs/configuration.md website/src/content/docs/architecture.md documentation/architecture.md
git commit -m "docs: document TBF scope-role learning knobs"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Headers → model only | Task 2 |
| Soft PW 429 / hard model 429 | Tasks 1–2 |
| Retry-After model-only | Task 2 (unchanged path; covered by existing + asymmetric tests) |
| Faster PW nudge | Tasks 1–2 |
| New env knobs | Tasks 1, 4 |
| `role` on `list_groups` | Task 3 |
| Dashboard labeling | Task 3 |
| Unit tests | Tasks 1–3 |
| Config / architecture / `.env.example` | Task 4 |
| AND-gate unchanged | No task (constraint; regressions in Task 2 step 4) |

## Acceptance check

After all tasks:

```bash
pytest tests/test_rate_limiter.py -v
```

Expected: all PASS. Manually open Rate limits page and confirm estimate/authoritative pills and updated blurb.
