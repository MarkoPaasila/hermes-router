# Comparable Headroom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cap-scaled comparable headroom (local binding window ÷ global model-scope max cap) for routing rank and `/v1/capacity`.

**Architecture:** Extend `BucketGroup` / `AdaptiveRateLimiter` with comparable scoring; keep raw `headroom()` for learning and thin thresholds. Router sort and capacity candidates switch to comparable reads. `list_groups` exposes both metrics.

**Tech Stack:** Python 3, pytest, existing Flask router.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-11-comparable-headroom-design.md`
- Peer max caps from **model-scope** groups only (exclude provider-wide)
- Binding window = `argmin` raw `tokens/cap` among active buckets
- Thin-threshold / learn / CSV keep raw fractional headroom
- Do not retune `/v1/capacity` advice bands
- Out of scope: request-equivalent conversion, dashboard redesign

## File Structure

| File | Responsibility |
|------|----------------|
| `rate_limiter.py` | `BucketGroup.comparable_headroom`, pool max scan, limiter APIs |
| `router.py` | Rank + capacity candidates use comparable scores |
| `tests/test_rate_limiter.py` | Unit tests for comparable scoring |
| `tests/test_capacity_endpoint.py` | Capacity still wired (mock comparable if needed) |
| `documentation/monitoring.md` | Note capacity headroom is comparable |

---

### Task 1: BucketGroup comparable_headroom + limiter APIs

**Files:**
- Modify: `rate_limiter.py`
- Modify: `tests/test_rate_limiter.py`

**Interfaces:**
- Produces: `BucketGroup.comparable_headroom(self, pool_max_caps: dict[str, float]) -> float`
- Produces: `AdaptiveRateLimiter._pool_max_caps_unlocked(self) -> dict[str, float]`
- Produces: `AdaptiveRateLimiter.comparable_headroom(provider, key, model) -> float` (model-scope)
- Produces: `AdaptiveRateLimiter.rank_comparable_headroom(provider, key, model) -> float` (min PW, model)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_rate_limiter.py`:

```python
def test_comparable_headroom_prefers_absolute_remaining():
    """Large tier at 20% beats tiny tier at 90% on same binding window."""
    from rate_limiter import AdaptiveRateLimiter
    from pathlib import Path
    import tempfile
    d = tempfile.mkdtemp()
    lim = AdaptiveRateLimiter(state_file=Path(d) / "rate.json")
    # Create two model groups via consume path or direct _groups
    # tiny: RPM cap 10, tokens 9 (90%); large: RPM cap 100, tokens 20 (20%)
    # Both TPM full so RPM binds for tiny; for large make RPM the lower % too
    ...
    assert lim.comparable_headroom("p", "k1", "m1") < lim.comparable_headroom("p", "k2", "m2")


def test_comparable_rpm_bound_not_crushed_by_peer_tpm():
    """Peer with huge TPM must not crush RPM-bound group via naive min-across-types."""
    ...


def test_comparable_blocked_is_zero():
    ...


def test_comparable_missing_group_is_one():
    lim = AdaptiveRateLimiter(state_file=Path(d) / "rate.json")
    assert lim.comparable_headroom("x", "k", "m") == 1.0


def test_rank_comparable_takes_min_of_pw_and_model():
    ...
```

(Use direct `BucketGroup` construction + inject into `lim._groups` with correct group keys from `AdaptiveRateLimiter._group_key` for focused tests.)

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_rate_limiter.py -k comparable -v`

- [ ] **Step 3: Implement**

On `BucketGroup`:

```python
def comparable_headroom(self, pool_max_caps: dict[str, float]) -> float:
    if time.time() < self.blocked_until:
        return 0.0
    active = [(n, b) for n, b in self.buckets.items() if b.active]
    if not active:
        return 1.0
    now = time.time()
    for _, b in active:
        b.refill(now)
    name, b = min(active, key=lambda x: x[1].headroom())
    max_cap = float(pool_max_caps.get(name, 0.0) or 0.0)
    if max_cap <= 0:
        return b.headroom()
    return max(0.0, min(1.0, b.tokens / max_cap))
```

On `AdaptiveRateLimiter`:

```python
def _pool_max_caps_unlocked(self) -> dict[str, float]:
    maxima = {k: 0.0 for k in ALL_LIMIT_KEYS}
    for gk, g in self._groups.items():
        if "|model:" not in gk:
            continue
        for name, b in g.buckets.items():
            if name in maxima and float(b.cap) > maxima[name]:
                maxima[name] = float(b.cap)
    return maxima

def comparable_headroom(self, provider_name: str, key: str, model: str) -> float:
    with self._lock:
        mg = self._groups.get(self._group_key(provider_name, key, model))
        if mg is None:
            return 1.0
        return mg.comparable_headroom(self._pool_max_caps_unlocked())

def rank_comparable_headroom(self, provider_name: str, key: str, model: str) -> float:
    with self._lock:
        caps = self._pool_max_caps_unlocked()
        pw = self._groups.get(self._group_key(provider_name, key, None))
        mg = self._groups.get(self._group_key(provider_name, key, model))
        if pw is None and mg is None:
            return 1.0
        scores = []
        if pw is not None:
            scores.append(pw.comparable_headroom(caps))
        if mg is not None:
            scores.append(mg.comparable_headroom(caps))
        return min(scores) if scores else 1.0
```

In `list_groups`, after computing raw headroom/binding, set:

```python
"comparable_headroom": (
    round(g.comparable_headroom(self._pool_max_caps_unlocked()), 3)
    if active else None
),
```

(Call `_pool_max_caps_unlocked` once per `list_groups` outside the loop for efficiency.)

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_rate_limiter.py -k comparable -v`

- [ ] **Step 5: Commit**

```bash
git add rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add cap-scaled comparable headroom on TBF groups"
```

---

### Task 2: Wire routing rank and capacity candidates

**Files:**
- Modify: `router.py` (sort ~1985; `_capacity_candidates` ~2522)
- Modify: `documentation/monitoring.md` (capacity headroom semantic)
- Modify: `tests/test_tool_routing.py` if it mocks `headroom` for sort — update mock name if needed

- [ ] **Step 1: Switch call sites**

Ranking:

```python
_rate_score = 1.0 - rate_limiter.rank_comparable_headroom(name, _peek_key, model)
```

Capacity:

```python
headroom = rate_limiter.comparable_headroom(name, key, model)
```

Keep raw `rate_limiter.headroom(...)` for thin-headroom logs / explore.

- [ ] **Step 2: Docs**

In `documentation/monitoring.md` capacity section, note that `components.headroom` is **comparable** (remaining on binding window ÷ global model-scope max cap for that window), not raw fill fraction.

- [ ] **Step 3: Run targeted tests**

Run: `pytest tests/test_rate_limiter.py -k comparable tests/test_capacity.py tests/test_capacity_endpoint.py tests/test_tool_routing.py -q`

- [ ] **Step 4: Commit**

```bash
git add router.py documentation/monitoring.md tests/
git commit -m "feat: use comparable headroom for rank and capacity"
```

---

## Spec coverage

| Spec requirement | Task |
|------------------|------|
| Cap-scaled binding score | 1 |
| Model-scope peer max only | 1 |
| `comparable_headroom` / `rank_comparable_headroom` | 1 |
| list_groups field | 1 |
| Routing + capacity call sites | 2 |
| Raw headroom unchanged for thin/learn | 2 (no change to those paths) |
| Docs semantic note | 2 |
| No advice retune / no dashboard redesign | honored (out of scope) |
