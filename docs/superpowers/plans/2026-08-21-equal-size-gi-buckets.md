# Equal-size GI Complexity Buckets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive complexity→min-GI cut points from catalog-relative percentiles so the five bands have approximately equal `(provider, model)` headcount.

**Architecture:** `gi_ranking` owns cached mins + `recompute_complexity_thresholds(scores)`; router collects GIs from `PROVIDERS`, marks dirty on catalog/GI mutations, and refreshes before selection/status.

**Tech Stack:** Python 3, Flask, pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-21-equal-size-gi-buckets-design.md`
- Complexity 1 → min GI always `0`
- Nearest-rank percentiles at 20/40/60/80 for complexity 2–5
- Population: every `(provider, model)` in catalog including GI default `0`
- No fallback to fixed `0/20/40/60/80` ladder
- Out of scope: complexity classifier, GI snapshot sources, selection sort keys beyond min-GI source

## File Structure

| File | Responsibility |
|------|----------------|
| `gi_ranking.py` | Percentile recompute, dirty flag, `min_gi_for_complexity`, threshold getter |
| `router.py` | Collect catalog GIs; dirty on model-list changes; refresh in selection/status |
| `tests/test_gi_ranking.py` | Unit tests for recompute / min_gi |
| `tests/test_selection_scales.py` | Selection tests against recomputed or mocked mins |
| `docs/adr/0002-general-intelligence-ranking.md` | Document percentile mapping |
| `documentation/routing.md` | Operator-facing note |

---

### Task 1: Percentile thresholds in `gi_ranking`

**Files:**
- Modify: `gi_ranking.py`
- Modify: `tests/test_gi_ranking.py`
- Modify: `tests/test_selection_scales.py`

**Interfaces:**
- Produces:
  - `mark_complexity_thresholds_dirty() -> None`
  - `complexity_thresholds_need_refresh() -> bool`
  - `recompute_complexity_thresholds(scores: list[float]) -> dict[int, float]`
  - `complexity_min_gi_map() -> dict[int, float]` (copy of cached 1..5)
  - `min_gi_for_complexity(c: int) -> float` (unchanged signature; reads cache)
- Consumes: none from router

- [ ] **Step 1: Write failing tests**

```python
import math

def _nearest_rank(sorted_scores, p):
    n = len(sorted_scores)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, math.ceil(p / 100.0 * n) - 1))
    return sorted_scores[idx]

def test_recompute_empty_all_zero():
    m = gi.recompute_complexity_thresholds([])
    assert m == {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
    assert gi.min_gi_for_complexity(1) == 0.0
    assert gi.min_gi_for_complexity(5) == 0.0

def test_recompute_even_ladder():
    scores = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0]
    m = gi.recompute_complexity_thresholds(scores)
    assert m[1] == 0.0
    assert m[2] == 20.0
    assert m[3] == 40.0
    assert m[4] == 60.0
    assert m[5] == 80.0

def test_recompute_tiny_n_bars_may_coincide():
    m = gi.recompute_complexity_thresholds([42.0])
    assert m[1] == 0.0
    assert m[2] == m[3] == m[4] == m[5] == 42.0

def test_complexity_1_always_zero_even_if_scores_high():
    m = gi.recompute_complexity_thresholds([90.0, 95.0, 99.0])
    assert m[1] == 0.0

def test_mark_dirty_and_need_refresh():
    gi.recompute_complexity_thresholds([0.0, 50.0, 100.0])
    assert gi.complexity_thresholds_need_refresh() is False
    gi.mark_complexity_thresholds_dirty()
    assert gi.complexity_thresholds_need_refresh() is True
```

Replace `test_min_gi_for_complexity` / `test_min_gi_thresholds` to recompute first (not assume fixed 40/80).

- [ ] **Step 2: Run tests — expect FAIL** (functions missing)

Run: `pytest tests/test_gi_ranking.py::test_recompute_empty_all_zero tests/test_gi_ranking.py::test_recompute_even_ladder -v`

- [ ] **Step 3: Implement in `gi_ranking.py`**

```python
import math

_complexity_min_gi: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
_complexity_thresholds_dirty: bool = True

def mark_complexity_thresholds_dirty() -> None:
    global _complexity_thresholds_dirty
    with _lock:
        _complexity_thresholds_dirty = True

def complexity_thresholds_need_refresh() -> bool:
    with _lock:
        return _complexity_thresholds_dirty

def _percentile_nearest_rank(sorted_scores: list[float], p: float) -> float:
    n = len(sorted_scores)
    if n == 0:
        return 0.0
    idx = int(math.ceil(p / 100.0 * n)) - 1
    idx = max(0, min(n - 1, idx))
    return float(sorted_scores[idx])

def recompute_complexity_thresholds(scores: list[float]) -> dict[int, float]:
    sorted_scores = sorted(float(s) for s in scores)
    out = {1: 0.0}
    for c, p in ((2, 20.0), (3, 40.0), (4, 60.0), (5, 80.0)):
        out[c] = _percentile_nearest_rank(sorted_scores, p)
    with _lock:
        global _complexity_min_gi, _complexity_thresholds_dirty
        _complexity_min_gi = dict(out)
        _complexity_thresholds_dirty = False
    log.info(
        "[gi] complexity min-GI thresholds n=%d → %s",
        len(sorted_scores),
        {k: round(v, 2) for k, v in out.items()},
    )
    return dict(out)

def complexity_min_gi_map() -> dict[int, float]:
    with _lock:
        return dict(_complexity_min_gi)

def min_gi_for_complexity(complexity: int) -> float:
    c = int(complexity)
    with _lock:
        if c in _complexity_min_gi:
            return _complexity_min_gi[c]
        if c <= 1:
            return _complexity_min_gi[1]
        return _complexity_min_gi[5]
```

- Remove live use of `COMPLEXITY_MIN_GI` (delete constant or leave unused — prefer delete).
- On successful reload in `load_snapshot` / `load_overrides` (paths that do not early-return), call `mark_complexity_thresholds_dirty()`.
- In `set_override` / `clear_override` (when state changes), mark dirty.
- `reset_for_tests`: reset map to all `0`, dirty `True`.

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_gi_ranking.py tests/test_selection_scales.py::test_min_gi_thresholds -v`

- [ ] **Step 5: Commit**

```bash
git add gi_ranking.py tests/test_gi_ranking.py tests/test_selection_scales.py
git commit -m "feat: derive complexity min-GI from catalog percentiles"
```

---

### Task 2: Router dirty refresh + status + selection tests

**Files:**
- Modify: `router.py`
- Modify: `tests/test_selection_scales.py`
- Create or extend: `tests/test_gi_ranking.py` (dirty→refresh via router helper if tested there) / selection tests

**Interfaces:**
- Consumes: `mark_complexity_thresholds_dirty`, `complexity_thresholds_need_refresh`, `recompute_complexity_thresholds`, `complexity_min_gi_map`, `resolve_gi`
- Produces: `_catalog_gi_scores(providers)`, `_refresh_complexity_thresholds_if_needed(providers=None)`

- [ ] **Step 1: Helpers + hooks**

```python
def _catalog_gi_scores(providers: list) -> list[float]:
    scores = []
    for p in providers:
        for m in (p.get("models") or [p.get("model") or ""]):
            if not m:
                continue
            gi, _ = gi_ranking.resolve_gi(p["name"], m)
            scores.append(gi)
    return scores

def _refresh_complexity_thresholds_if_needed(providers: list | None = None) -> None:
    if not gi_ranking.complexity_thresholds_need_refresh():
        return
    src = providers if providers is not None else PROVIDERS
    gi_ranking.recompute_complexity_thresholds(_catalog_gi_scores(src))
```

- Call `_refresh_complexity_thresholds_if_needed(providers)` at start of `_get_smart_ordered` (use the `providers` arg so tests and live path share one code path; live callers pass `PROVIDERS`).
- Call `_refresh_complexity_thresholds_if_needed()` in `status()` before building the payload; add top-level `"complexity_min_gi": gi_ranking.complexity_min_gi_map()`.
- After any mutation of a provider's `models` list (discovery refresh, catalog restore, exclude that replaces the list), call `gi_ranking.mark_complexity_thresholds_dirty()`.
- After dashboard `set_override` / `clear_override` routes succeed, mark dirty (also marked inside gi_ranking — redundant OK).

- [ ] **Step 2: Fix selection tests**

Before `_get_smart_ordered` in tests that assume classic bars, either:

```python
gi_ranking.recompute_complexity_thresholds([0.0, 20.0, 40.0, 60.0, 80.0, 100.0])
```

or monkeypatch `min_gi_for_complexity`. Prefer recompute so dirty is clear and `_get_smart_ordered` does not overwrite: ensure `complexity_thresholds_need_refresh()` is False after recompute; if the helper refreshes from the small test provider list, seed PROVIDERS via monkeypatch or pass providers and accept percentile-from-test-catalog semantics.

**Chosen rule for `_get_smart_ordered`:** refresh from the **`providers` argument** when dirty (matches “candidates being ranked” in tests; production always passes `PROVIDERS`). Document in code comment.

For `test_cheapest_among_eligible` / hard/easy tests: recompute ladder first **and** monkeypatch `complexity_thresholds_need_refresh` to False so the helper does not rebuild from the 2-model list; OR monkeypatch `_refresh_complexity_thresholds_if_needed` to no-op after seeding.

Simplest: monkeypatch `router._refresh_complexity_thresholds_if_needed` to no-op in selection tests, and `recompute_complexity_thresholds([0,20,40,60,80,100])` for classic mins.

Add one test: dirty + two providers with GIs → refresh uses those scores → complexity 5 min equals 80th percentile of that set.

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_gi_ranking.py tests/test_selection_scales.py -v`

- [ ] **Step 4: Commit**

```bash
git add router.py tests/test_selection_scales.py tests/test_gi_ranking.py
git commit -m "feat: refresh GI complexity thresholds from live catalog"
```

---

### Task 3: Docs

**Files:**
- Modify: `docs/adr/0002-general-intelligence-ranking.md`
- Modify: `documentation/routing.md` (and configuration snippet if it still cites `0/20/40/60/80`)

- [ ] Replace “defaults 0/20/40/60/80” with catalog-relative percentile mins; note refresh triggers and `/v1/status` field `complexity_min_gi`.
- [ ] Commit: `docs: document equal-size GI complexity buckets`

---

### Task 4: Full verification

- [ ] Run: `pytest tests/test_gi_ranking.py tests/test_selection_scales.py -v`
- [ ] Confirm no remaining references assuming live fixed ladder (grep `COMPLEXITY_MIN_GI`, `min_gi_for_complexity(5) == 80`).
