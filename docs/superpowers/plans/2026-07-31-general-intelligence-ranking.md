# General Intelligence Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Capability (1–5) with continuous GI (0–100) from a median-normalized LMSYS + Artificial Analysis snapshot, with dashboard overrides and complexity→min-GI selection.

**Architecture:** New `gi_ranking.py` owns snapshot load, override persistence, resolution, and thresholds. `router.py` selection/status/UI call into it. A maintainer script builds `gi_rankings.json` from source JSON inputs. No live leaderboard fetch in the proxy.

**Tech Stack:** Python 3, pytest, Flask, existing dashboard JS in `router.py`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-31-general-intelligence-ranking-design.md`
- GI scale 0–100; higher = stronger
- Resolve: override → snapshot → `0`
- Complexity min GI defaults: 1→0, 2→20, 3→40, 4→60, 5→80
- Selection: cheapest among `gi >= min_gi`; drop `MODEL_QUALITY_RANKS` from sort
- Wire field `gi` + `gi_source`; do not map legacy 1–5 `rating` into GI
- User copy: General intelligence / GI; feature probing (not capability) for tools/vision

## File Structure

| File | Responsibility |
|------|----------------|
| `gi_ranking.py` | Snapshot, overrides, resolve, thresholds, median helper |
| `gi_rankings.json` | Checked-in default scores |
| `gi_overrides.json` | Runtime overrides (gitignored) |
| `scripts/refresh_gi_rankings.py` | Build snapshot from source JSON files |
| `router.py` | Wire selection, status, API, dashboard |
| `tests/test_gi_ranking.py` | Unit tests for resolve/median/thresholds/overrides |
| `tests/test_selection_scales.py` | Update selection tests for GI |
| `CONTEXT.md`, `documentation/*`, `README.md`, `docs/adr/*` | Glossary + docs |
| `.gitignore`, `.env.example` | Overrides file ignore + env knobs |

---

### Task 1: GI module + median helper + snapshot seed

**Files:** Create `gi_ranking.py`, `gi_rankings.json`, `tests/test_gi_ranking.py`; Modify `.gitignore`

**Interfaces:**
- Produces: `median_normalized(scores: list[float]) -> float`, `min_gi_for_complexity(c: int) -> float`, `resolve_gi(provider: str, model: str) -> tuple[float, str]`, `set_override` / `clear_override` / `load_snapshot` / `load_overrides`

- [ ] **Step 1: Write failing tests** in `tests/test_gi_ranking.py` for median of one/two/three values, resolve order, thresholds, override set/clear, out-of-range reject.

- [ ] **Step 2: Implement `gi_ranking.py`** with defaults, file I/O, longest-substring snapshot match, override key `provider|model`.

- [ ] **Step 3: Seed `gi_rankings.json`** from known model families mapped roughly onto 0–100 (refresh script will replace with real leaderboard medians later).

- [ ] **Step 4: `pytest tests/test_gi_ranking.py -q`** — pass; commit.

---

### Task 2: Selection wiring in router

**Files:** Modify `router.py` (`_get_smart_ordered`, `_model_caps`, discovery sort, remove quality ranks); Modify `tests/test_selection_scales.py`

- [ ] **Step 1: Update tests** to use `gi` / monkeypatch `resolve_gi` or `_model_caps` with `gi`.

- [ ] **Step 2: Replace rating comparison** with `min_gi_for_complexity` + `gi`; remove `MODEL_QUALITY_RANKS` / `_quality_rank` from sort key; `_model_caps` returns `gi`, `gi_source` (drop strength `rating` from caps or leave unset).

- [ ] **Step 3: `pytest tests/test_selection_scales.py tests/test_gi_ranking.py -q` — pass; commit.

---

### Task 3: Override API + dashboard UI

**Files:** Modify `router.py` (routes + Models modal HTML/JS); Modify `.env.example`

- [ ] **Step 1: Add** `PUT`/`DELETE` `/v1/config/gi-override`; status `model_caps` include `gi`/`gi_source`.

- [ ] **Step 2: UI** — replace Capability pips with GI number + source; modal set/clear.

- [ ] **Step 3: Manual smoke or API tests**; commit.

---

### Task 4: Refresh script

**Files:** Create `scripts/refresh_gi_rankings.py`

- [ ] **Step 1: Script** reads optional `lmsys.json` / `aa.json` inputs (list of `{id, score}`), normalizes per source, median-combines, writes `gi_rankings.json`.

- [ ] **Step 2: Commit**.

---

### Task 5: Docs + ADR + gitignore

**Files:** `CONTEXT.md`, `documentation/routing.md`, `documentation/architecture.md`, `documentation/configuration.md`, `documentation/monitoring.md`, `README.md`, `docs/adr/0002-general-intelligence-ranking.md`, update ADR 0001 note, `.gitignore`

- [ ] **Step 1: Glossary + docs rename**; ADR 0002; ignore `gi_overrides.json`.

- [ ] **Step 2: Full pytest**; commit.
