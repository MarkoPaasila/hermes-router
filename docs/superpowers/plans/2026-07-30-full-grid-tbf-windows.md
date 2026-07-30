# Full-Grid Multi-Window TBF Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Always track all ten `[R,T]×[M,H,D,W,Mo]` token buckets on every provider-wide and model group, with linear-scale init (explicit quotas win), PW tiny window-scaled soft ticks on every 429, M-only success nudges, no long-window auto-deactivate, and soft ranking only (no thin-headroom hard-skip).

**Architecture:** Expand `_load_caps_for` / `_caps_for` into a full-grid prior, keep dual AND-gate consume debiting every bucket, change soft `BucketGroup.on_429` to tick all PW buckets with `ε × (T_M / T_window)`, restrict `on_success` nudges to minute windows, persist/backfill the full grid, and align embeddings routing with chat’s attempt-on-thin behavior.

**Tech Stack:** Python 3, `rate_limiter.py`, Flask `router.py`, pytest, markdown docs, `.env.example`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-30-full-grid-tbf-windows-design.md`
- Both scopes always own all 10 limit keys in `LIMIT_KEYS`
- Explicit table/env/auth quotas override linear scale; ordering `Mo ≥ W ≥ D ≥ H ≥ M` with explicit values sticky
- Provider-wide ×10 (`RATE_PROVIDER_CAP_MULTIPLIER`) applies only when creating a new PW group — never on `load()`
- Model: binding hard 429 + headers + Retry-After unchanged
- Provider-wide: no headers, no Retry-After; every model 429 → tiny soft tick on **each** PW bucket
- Long windows: no routine success-streak nudge from a cold prior
- Thin headroom never hard-skips a candidate that can still afford the debit
- No synthetic probe traffic

---

## File Structure

| File | Responsibility |
|---|---|
| `rate_limiter.py` | Full-grid expand/clamp; PW tick ε; soft-on-all; M-only nudge; no inactive; persist/backfill |
| `router.py` | Embeddings: attempt on thin headroom (match chat); keep rank-by-headroom soft preference |
| `tests/test_rate_limiter.py` | Grid, scale, ticks, nudge scope, persist/backfill, inactive no-op |
| `documentation/configuration.md` | Document full grid, PW tick, soft rank, M-only nudge |
| `documentation/architecture.md` | Brief TBF full-grid note |
| `website/src/content/docs/configuration.md` | Mirror |
| `website/src/content/docs/architecture.md` | Mirror |
| `.env.example` | Document `RATE_LEARN_PW_TICK_EPS` and updated headroom wording |

---

### Task 1: Full-grid cap expansion and always-on inventory

**Files:**
- Modify: `rate_limiter.py` (`WINDOWS`, `LIMIT_KEYS`, `_load_caps_for`, new helpers, `BucketGroup.__init__`, `AdaptiveRateLimiter._caps_for`)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: existing `WINDOWS`, `LIMIT_KEYS`, `PROVIDER_RATE_DEFAULTS`, `_load_caps_for`, `RATE_PROVIDER_CAP_MULTIPLIER`
- Produces:
  - `ALL_LIMIT_KEYS: tuple[str, ...]` — stable order of all ten names from `LIMIT_KEYS`
  - `expand_full_grid_caps(base: dict[str, float]) -> dict[str, float]` — fill missing windows from `M`, honor explicit values, enforce ordering with explicit sticky
  - `_load_caps_for(provider_name: str) -> dict[str, float]` — returns **full** 10-key dict after expand
  - `BucketGroup.__init__` — creates a bucket for every key in the expanded caps (always 10 when using `_load_caps_for` / `_caps_for`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rate_limiter.py`:

```python
FULL_LIMIT_KEYS = {
    "RPM", "RPH", "RPD", "RPW", "RPMo",
    "TPM", "TPH", "TPD", "TPW", "TPMo",
}


def test_load_caps_for_returns_full_grid():
    from rate_limiter import _load_caps_for, WINDOWS
    caps = _load_caps_for("openrouter")
    assert set(caps) == FULL_LIMIT_KEYS
    assert caps["TPH"] == pytest.approx(caps["TPM"] * (WINDOWS["H"] / WINDOWS["M"]))
    assert caps["TPMo"] == pytest.approx(caps["TPM"] * (WINDOWS["Mo"] / WINDOWS["M"]))
    assert caps["TPMo"] >= caps["TPW"] >= caps["TPD"] >= caps["TPH"] >= caps["TPM"]
    assert caps["RPMo"] >= caps["RPW"] >= caps["RPD"] >= caps["RPH"] >= caps["RPM"]


def test_explicit_rpd_overrides_linear_scale():
    from rate_limiter import _load_caps_for, WINDOWS
    caps = _load_caps_for("gemini")
    assert caps["RPD"] == pytest.approx(1500.0)
    # Must not be forced up to RPM * (day/minute)
    assert caps["RPD"] < caps["RPM"] * (WINDOWS["D"] / WINDOWS["M"])
    assert caps["RPD"] >= caps["RPH"] >= caps["RPM"]


def test_new_groups_always_have_ten_buckets(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    assert set(pw.buckets) == FULL_LIMIT_KEYS
    assert set(mg.buckets) == FULL_LIMIT_KEYS
    assert pw.buckets["TPM"].cap == pytest.approx(mg.buckets["TPM"].cap * 10.0)
    assert pw.buckets["TPMo"].cap == pytest.approx(mg.buckets["TPMo"].cap * 10.0)


def test_one_consume_debits_all_ten_and_mo_pct_drops_less(tmp_path):
    rl = make_limiter(tmp_path)
    rl.get_group("openrouter", "key-abc12345", "m")
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    for b in mg.buckets.values():
        b.tokens = b.cap
    tpm_before = mg.buckets["TPM"].tokens / mg.buckets["TPM"].cap
    tpmo_before = mg.buckets["TPMo"].tokens / mg.buckets["TPMo"].cap
    ok, _ = rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 500.0)
    assert ok is True
    tpm_after = mg.buckets["TPM"].tokens / mg.buckets["TPM"].cap
    tpmo_after = mg.buckets["TPMo"].tokens / mg.buckets["TPMo"].cap
    assert (tpm_before - tpm_after) > (tpmo_before - tpmo_after)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_load_caps_for_returns_full_grid \
  tests/test_rate_limiter.py::test_explicit_rpd_overrides_linear_scale \
  tests/test_rate_limiter.py::test_new_groups_always_have_ten_buckets \
  tests/test_rate_limiter.py::test_one_consume_debits_all_ten_and_mo_pct_drops_less \
  -v
```

Expected: FAIL (missing keys / expand helper not present).

- [ ] **Step 3: Implement expand + wire into `_load_caps_for`**

In `rate_limiter.py`, after `LIMIT_KEYS`:

```python
ALL_LIMIT_KEYS = tuple(LIMIT_KEYS.keys())
_WINDOW_ORDER = ("M", "H", "D", "W", "Mo")  # short → long


def _limit_name(dim: str, window: str) -> str:
    prefix = "R" if dim == "R" else "T"
    return f"{prefix}P{window}"  # RPM, RPH, …, RPMo


def expand_full_grid_caps(base: dict[str, float]) -> dict[str, float]:
    """Fill missing R/T windows from M; explicit values win; Mo≥…≥M with explicit sticky."""
    out: dict[str, float] = {}
    for dim in ("R", "T"):
        names = [_limit_name(dim, wk) for wk in _WINDOW_ORDER]
        if not any(n in base for n in names):
            continue
        m_name = _limit_name(dim, "M")
        if m_name in base:
            m_cap = float(base[m_name])
        else:
            # Derive M from the shortest explicit longer window
            m_cap = None
            for wk, n in zip(_WINDOW_ORDER, names):
                if n in base:
                    m_cap = float(base[n]) * (WINDOWS["M"] / WINDOWS[wk])
                    break
            if m_cap is None:
                continue
        caps_w: dict[str, float] = {}
        explicit: set[str] = set()
        for wk, n in zip(_WINDOW_ORDER, names):
            if n in base:
                caps_w[n] = float(base[n])
                explicit.add(n)
            else:
                caps_w[n] = float(m_cap) * (WINDOWS[wk] / WINDOWS["M"])
        for i in range(1, len(names)):
            shorter, longer = names[i - 1], names[i]
            if caps_w[longer] >= caps_w[shorter]:
                continue
            if longer in explicit and shorter not in explicit:
                caps_w[shorter] = caps_w[longer]
            elif longer not in explicit:
                caps_w[longer] = caps_w[shorter]
            else:
                # both explicit and inverted — keep longer explicit, pull shorter down
                caps_w[shorter] = caps_w[longer]
        out.update(caps_w)
    for k, v in base.items():
        if k not in out:
            out[k] = float(v)
    return out
```

Change `_load_caps_for` to end with `return expand_full_grid_caps(base)`.

Ensure `AdaptiveRateLimiter._caps_for` still multiplies the **full** dict by ×10 when `provider_wide=True`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_load_caps_for_returns_full_grid \
  tests/test_rate_limiter.py::test_explicit_rpd_overrides_linear_scale \
  tests/test_rate_limiter.py::test_new_groups_always_have_ten_buckets \
  tests/test_rate_limiter.py::test_one_consume_debits_all_ten_and_mo_pct_drops_less \
  tests/test_rate_limiter.py::test_new_provider_wide_caps_are_10x_model_defaults \
  -v
```

Expected: PASS (update `test_new_provider_wide_caps_are_10x_model_defaults` if it only checked RPM/TPM — still valid).

- [ ] **Step 5: Commit**

```bash
git add -f rate_limiter.py tests/test_rate_limiter.py
git commit -m "$(cat <<'EOF'
feat: always expand TBF groups to full R/T × M..Mo grid

EOF
)"
```

---

### Task 2: Provider-wide tiny soft ticks (all buckets, window-scaled)

**Files:**
- Modify: `rate_limiter.py` (`TokenBucket.on_429` soft path and/or `BucketGroup.on_429` when `soft=True`)
- Modify: `tests/test_rate_limiter.py`
- Modify: `.env.example` (document new env)

**Interfaces:**
- Consumes: `WINDOWS`, `LIMIT_KEYS`, existing soft floor
- Produces:
  - `RATE_LEARN_PW_TICK_EPS: float` — env `RATE_LEARN_PW_TICK_EPS`, default `0.05`
  - Soft `on_429` (provider-wide): for **every** bucket (not binding-filtered),  
    `new_cap = max(floor, cap * (1 - RATE_LEARN_PW_TICK_EPS * (WINDOWS["M"] / window_seconds)))`  
    then zero tokens / reset streak as today; reason `"soft_429"` or `"soft_tick"`
  - Hard `on_429` (model): **unchanged** binding-selective hard cut

- [ ] **Step 1: Write the failing tests**

```python
def test_soft_429_ticks_all_buckets_including_high_headroom():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    g.buckets["TPM"].tokens = 0.0                     # binding-looking
    g.buckets["TPMo"].tokens = g.buckets["TPMo"].cap  # full
    tpm0 = g.buckets["TPM"].cap
    tpmo0 = g.buckets["TPMo"].cap
    g.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)
    assert g.buckets["TPM"].cap < tpm0
    assert g.buckets["TPMo"].cap < tpmo0  # must tick even when not binding
    assert (tpm0 - g.buckets["TPM"].cap) / tpm0 > (tpmo0 - g.buckets["TPMo"].cap) / tpmo0


def test_pw_soft_tick_via_limiter_no_retry_after(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    rl.get_group("openrouter", "key-abc12345", "m")
    for b in pw.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 0.0
    tpm0, tpmo0 = pw.buckets["TPM"].cap, pw.buckets["TPMo"].cap
    rl.on_429("openrouter", "key-abc12345", "m", {}, model_headroom_before=0.5)
    assert pw.buckets["TPM"].cap < tpm0
    assert (tpm0 - pw.buckets["TPM"].cap) / tpm0 > (tpmo0 - pw.buckets["TPMo"].cap) / tpmo0
    assert pw.blocked_until <= time.time()


def test_many_soft_ticks_eventually_move_mo():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    before = g.buckets["TPMo"].cap
    for _ in range(200):
        g.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)
    assert g.buckets["TPMo"].cap < before * 0.99
```
- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_soft_429_ticks_all_buckets_including_high_headroom \
  tests/test_rate_limiter.py::test_pw_soft_tick_moves_m_more_than_mo_fractionally \
  -v
```

Expected: FAIL (soft path still binding-selective and/or large cut factors).

- [ ] **Step 3: Implement soft tick**

Add:

```python
RATE_LEARN_PW_TICK_EPS = _float_env("RATE_LEARN_PW_TICK_EPS", 0.05)
```

Change `TokenBucket.on_429` to accept optional `tick_eps: float | None = None`. When `soft=True` and `tick_eps is not None`:

```python
frac = max(0.0, min(1.0, tick_eps * (60.0 / self.window_seconds)))
new_cap = max(1.0, self.cap * (1.0 - frac))
# then soft floor as today
```

When `soft=True` and `tick_eps is None`, keep legacy soft cut for any leftover callers **or** always pass tick_eps from `BucketGroup`.

Change `BucketGroup.on_429`:

```python
if soft:
    for name, b in self.buckets.items():
        if name in header_touched:
            continue
        if not b.active:
            b.active = True
        change = b.on_429(observed_rate=b._period_consumed, soft=True,
                          tick_eps=RATE_LEARN_PW_TICK_EPS)
        ...
    # retry-after block unchanged (caller passes False for PW)
    ...
else:
    # existing binding-selective hard cut path
    ...
```

Surprise path already calls `pw.on_429(..., soft=True)` again — that becomes a second round of tiny ticks (allowed by spec).

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest tests/test_rate_limiter.py -v -k "soft_429 or pw_soft or on_429"
```

Expected: PASS; fix any tests that assumed soft cut only hits binding buckets or uses `RATE_LEARN_SOFT_CUT_FACTOR` magnitude.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py .env.example
git commit -m "$(cat <<'EOF'
feat: apply window-scaled tiny soft ticks to all PW TBF buckets

EOF
)"
```

---

### Task 3: Minute-only success nudges

**Files:**
- Modify: `rate_limiter.py` (`BucketGroup.on_success` or `TokenBucket` gate)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `LIMIT_KEYS` / window key
- Produces: `on_success` only nudges buckets whose window is `M` (`RPM`, `TPM`). H/D/W/Mo never routine-nudge.

- [ ] **Step 1: Write the failing test**

```python
def test_on_success_nudges_only_minute_windows():
    from rate_limiter import BucketGroup, _load_caps_for, RATE_LEARN_SUCCESS_STREAK
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    for b in g.buckets.values():
        b._header_pinned = False
        b._consecutive_successes = RATE_LEARN_SUCCESS_STREAK - 1
    before = {n: b.cap for n, b in g.buckets.items()}
    changes = g.on_success(100.0)
    changed = {n for n, _ in changes}
    assert changed <= {"RPM", "TPM"}
    assert "TPM" in changed or "RPM" in changed
    for n in ("TPH", "TPD", "TPW", "TPMo", "RPH", "RPD", "RPW", "RPMo"):
        assert g.buckets[n].cap == pytest.approx(before[n])
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_on_success_nudges_only_minute_windows -v
```

Expected: FAIL (longer windows also nudge).

- [ ] **Step 3: Implement**

In `BucketGroup.on_success`:

```python
for name, b in self.buckets.items():
    if not b.active:
        continue
    _dim, wk = LIMIT_KEYS[name]
    if wk != "M":
        continue
    change = b.on_success(...)
```

- [ ] **Step 4: Run tests**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_on_success_nudges_only_minute_windows \
  tests/test_rate_limiter.py -k "on_success" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "$(cat <<'EOF'
feat: restrict TBF success nudges to minute windows only

EOF
)"
```

---

### Task 4: Disable long-window auto-inactive; persist and backfill full grid

**Files:**
- Modify: `rate_limiter.py` (`TokenBucket.check_inactive`, `BucketGroup.to_dict` / `from_dict` / `run_inactive_check`, `AdaptiveRateLimiter.load`)
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Consumes: `expand_full_grid_caps`, `_caps_for`
- Produces:
  - `check_inactive` is a no-op (always leaves `active=True`) **or** `run_inactive_check` no longer deactivates
  - `BucketGroup.to_dict` persists **all** buckets (not only `active`)
  - `load()` / group restore **backfills** missing limit keys via `expand_full_grid_caps` + base caps without re-×10 on existing PW caps

- [ ] **Step 1: Write the failing tests**

```python
def test_check_inactive_never_deactivates_long_window():
    b = TokenBucket(window_seconds=86400.0, cap=100.0, tokens=100.0)
    b.check_inactive(activity=0)
    assert b.active is True


def test_to_dict_persists_all_ten_buckets():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    d = g.to_dict()
    assert FULL_LIMIT_KEYS <= set(k for k, v in d.items() if isinstance(v, dict))


def test_load_backfills_missing_windows(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    # Simulate legacy state with only RPM/TPM
    gk = rl._group_key("openrouter", "key-abc12345", None)
    rl._groups[gk].buckets = {
        "RPM": pw.buckets["RPM"],
        "TPM": pw.buckets["TPM"],
    }
    learned_tpm = 12345.0
    rl._groups[gk].buckets["TPM"].cap = learned_tpm
    rl.flush()
    rl2 = make_limiter(tmp_path)
    rl2.load()
    pw2 = rl2.get_group("openrouter", "key-abc12345", None)
    assert set(pw2.buckets) == FULL_LIMIT_KEYS
    assert pw2.buckets["TPM"].cap == pytest.approx(learned_tpm)
    assert "TPMo" in pw2.buckets
```

Update/replace obsolete tests:

- `test_inactive_after_quiet_period` → expect `active is True`
- `test_run_all_inactive_checks_marks_quiet_buckets_inactive` → expect buckets stay active

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest \
  tests/test_rate_limiter.py::test_check_inactive_never_deactivates_long_window \
  tests/test_rate_limiter.py::test_to_dict_persists_all_ten_buckets \
  tests/test_rate_limiter.py::test_load_backfills_missing_windows \
  -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

```python
def check_inactive(self, activity: float) -> None:
    """No-op: full-grid design keeps all windows binding for debit/rank."""
    return
```

```python
def to_dict(self) -> dict:
    out = {name: b.to_dict() for name, b in self.buckets.items()}
    ...
```

In `AdaptiveRateLimiter.load`, after restoring a group (and after any PW migration), call a helper:

```python
def _backfill_group_buckets(self, g: BucketGroup, provider_name: str, *, provider_wide: bool) -> None:
    base = self._caps_for(provider_name, provider_wide=False)  # full grid, unmultiplied
    # For missing keys only: use base (model) or base*mult for PW *new* keys
    mult = RATE_PROVIDER_CAP_MULTIPLIER if provider_wide else 1.0
    for name, cap in base.items():
        if name in g.buckets:
            continue
        dim, wk = LIMIT_KEYS[name]
        b = TokenBucket(window_seconds=WINDOWS[wk], cap=float(cap) * mult, dimension=dim)
        b._floor_cap = float(base[name])
        g.buckets[name] = b
```

Do **not** multiply caps that were loaded from state.

- [ ] **Step 4: Run full rate limiter suite**

```bash
cd /home/marko/Projektit/Hermes-router && python -m pytest tests/test_rate_limiter.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "$(cat <<'EOF'
feat: keep full TBF grid active and backfill on state load

EOF
)"
```

---

### Task 5: Soft rank only — fix embeddings thin-headroom hard-skip

**Files:**
- Modify: `router.py` (embeddings loop ~5989–5992; confirm chat path already attempts)
- Test: add a focused unit/integration test if one exists for embeddings routing; otherwise a small extracted-behavior test or document manual check. Prefer a router test if `tests/` already mocks embeddings cascade.

**Interfaces:**
- Consumes: `RATE_HEADROOM_THRESHOLD`, `rate_limiter.headroom`, `check_and_consume`
- Produces: embeddings path logs thin headroom and **attempts** `check_and_consume` (same as chat ~5510–5515); only skips when consume fails / wait too long

- [ ] **Step 1: Confirm chat behavior and write failing test if feasible**

Chat already:

```python
if _current_headroom < RATE_HEADROOM_THRESHOLD:
    log.debug(... attempting)
# then check_and_consume
```

Embeddings currently `continue`s on thin headroom — change to match chat.

If no existing embeddings routing test harness is lightweight, skip automated router test and rely on the code change + manual note; still change the code.

Optional test in `tests/test_rate_limiter.py` is N/A — this is router policy. Search for embeddings tests:

```bash
rg -n "embeddings|thin headroom" tests/
```

If a test can monkeypatch `headroom` to `0.01` and assert forward is still attempted, write it; else proceed with code parity only.

- [ ] **Step 2: Implement**

Replace embeddings block:

```python
_current_headroom = rate_limiter.headroom(name, key, em)
if _current_headroom < RATE_HEADROOM_THRESHOLD:
    log.debug(f"  {name}/{em} thin headroom ({_current_headroom:.1%}) — attempting")
_rl_ok, _rl_wait = rate_limiter.check_and_consume(...)
```

Ranking already uses `1.0 - headroom` as soft score in candidate ordering — leave that.

- [ ] **Step 3: Grep for other thin hard-skips**

```bash
rg -n "RATE_HEADROOM_THRESHOLD" router.py
```

Ensure no remaining `continue` solely because headroom &lt; threshold.

- [ ] **Step 4: Commit**

```bash
git add router.py
git commit -m "$(cat <<'EOF'
fix: attempt embeddings on thin TBF headroom instead of hard-skip

EOF
)"
```

---

### Task 6: Documentation

**Files:**
- Modify: `documentation/configuration.md`, `documentation/architecture.md`
- Modify: `website/src/content/docs/configuration.md`, `website/src/content/docs/architecture.md`
- Modify: `.env.example` (if not fully done in Task 2)

**Interfaces:**
- Produces: docs matching spec behavior (full grid, PW tick ε, M-only nudge, soft rank, no inactive)

- [ ] **Step 1: Update configuration docs**

In the TBF / rate-limit section:

- State that every group always tracks all ten `[R,T]×[M,H,D,W,Mo]` buckets.
- Init: explicit defaults/env/auth win; else `Cap(W)=Cap(M)×(T_W/T_M)`; ordering Mo≥…≥M with explicit sticky.
- PW 429: tiny soft tick `ε × (T_M/T_window)` on every PW bucket (`RATE_LEARN_PW_TICK_EPS`, default `0.05`).
- Success nudges: minute windows only.
- `RATE_HEADROOM_THRESHOLD`: ranking / “thin” log signal only — not a hard skip.
- Long windows are not auto-deactivated.

Update the `RATE_HEADROOM_THRESHOLD` table row wording accordingly; add `RATE_LEARN_PW_TICK_EPS` row.

- [ ] **Step 2: Update architecture blurb**

One short paragraph: full-grid ledgers; minute is the fast learner; longer windows are stable priors moved by evidence / accumulated PW ticks.

- [ ] **Step 3: Mirror website docs**

Copy the same substance into `website/src/content/docs/configuration.md` and `architecture.md`.

- [ ] **Step 4: Commit**

```bash
git add documentation/configuration.md documentation/architecture.md \
  website/src/content/docs/configuration.md website/src/content/docs/architecture.md \
  .env.example
git commit -m "$(cat <<'EOF'
docs: document full-grid TBF windows and PW soft ticks

EOF
)"
```

---

## Self-Review (plan vs spec)

| Spec requirement | Task |
|---|---|
| Always-on 10 buckets both scopes | Task 1 |
| Explicit quotas win; else linear scale; ordering sticky | Task 1 |
| Same absolute debit; Mo % moves less | Task 1 test |
| PW tiny ticks every 429; longer moves less; accumulate | Task 2 |
| Model binding hard 429 / headers / Retry-After unchanged | Task 2 (hard path untouched) |
| No routine long-window success nudge | Task 3 |
| No auto-inactive; persist/backfill full grid | Task 4 |
| Soft rank; no thin hard-skip | Task 5 (embeddings); chat already OK |
| Docs | Task 6 |
| Surprise stays tiny-tick family | Task 2 (second soft `on_429`) |
| ×10 only on new PW create | Task 1 + Task 4 backfill rules |

No TBD/placeholder steps remain after implementers follow the concrete snippets. `expand_full_grid_caps` name helper must be implemented carefully once in Task 1 and reused.
