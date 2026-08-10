# Capacity Pace Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose `GET /v1/capacity` and `hr pace` so Hermes Agent cron can stretch intervals or skip ticks from a compact pool capacity score.

**Architecture:** Pure scoring in `capacity.py` (testable without Flask). Router gathers configured `(provider, model)` candidates, model-scope headroom / blocked_until from the rate limiter, and health from `ProviderStats`, then maps to advice. CLI mirrors `hr status`.

**Tech Stack:** Python 3, Flask, pytest, bash (`hr` / `scripts/pace.sh`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-10-capacity-pace-design.md`
- Model-scope headroom only (not provider-wide soft estimates)
- Same `_auth_check` as other `/v1/*`
- No upstream calls from `/v1/capacity`
- Fixed default thresholds (no env overrides in v1)
- Out of scope: Hermes Agent code, dashboard UI, batch sizing

## File Structure

| File | Responsibility |
|------|----------------|
| `capacity.py` | Pure score + advice mapping from candidate tuples |
| `rate_limiter.py` | Read-only model-scope headroom / blocked_until helpers |
| `router.py` | Gather candidates; `GET /v1/capacity` route |
| `tests/test_capacity.py` | Unit tests for scoring/advice |
| `tests/test_capacity_endpoint.py` | HTTP auth + response shape (or extend existing app tests) |
| `scripts/pace.sh` | `hr pace` client |
| `hermes-router` | Wire `pace` command + help |
| `documentation/monitoring.md` | Endpoint + Hermes cron contract |
| `documentation/architecture.md` | Endpoint table row |

---

### Task 1: Pure capacity scoring module + unit tests

**Files:**
- Create: `capacity.py`
- Create: `tests/test_capacity.py`

- [ ] Write failing tests for: no candidates → skip; thresholds (fast/normal/slow/skip); top-K mean; breaker/blocked → effective 0; fewer than 2 usable → skip; health_factor mapping
- [ ] Implement `capacity.py` with:
  - `HEALTH_FACTOR = {0: 1.0, 1: 0.7, 2: 0.3}`
  - `TOP_K = 3`, `USABLE_MIN = 0.05`, `USABLE_COUNT_FLOOR = 2`
  - `advice_for(capacity) -> (advice, multiplier, skip)`
  - `score_pool(candidates) -> dict` matching response fields (minus `generated_at`)
  - Candidate dict keys: `headroom`, `health_bucket`, `breaker_open`, `blocked` (bool)
- [ ] Run `pytest tests/test_capacity.py` — pass

---

### Task 2: Rate-limiter model-scope reads + `/v1/capacity` route

**Files:**
- Modify: `rate_limiter.py` (add `model_headroom` / `model_blocked` read-only helpers)
- Modify: `router.py`
- Create: `tests/test_capacity_endpoint.py` (or minimal Flask test)

- [ ] Add AdaptiveRateLimiter methods that read **model group only** (missing → headroom 1.0, not blocked)
- [ ] In router: build candidate list from `PROVIDERS` models + `pool.peek_key` + `rate_limiter` + `stats.health_bucket` / `breaker_open`
- [ ] Add `@app.route("/v1/capacity")` returning `score_pool` + `generated_at`
- [ ] HTTP test: 401 without auth; 200 with auth and required keys
- [ ] Run targeted pytest — pass

---

### Task 3: `hr pace` + documentation

**Files:**
- Create: `scripts/pace.sh`
- Modify: `hermes-router`
- Modify: `documentation/monitoring.md`
- Modify: `documentation/architecture.md`

- [ ] `scripts/pace.sh` mirrors status.sh auth/PORT; default one-liner; `--json` raw
- [ ] Wire `pace)` case + help comment in `hermes-router`
- [ ] Document curl/`hr pace` and Hermes cron contract in monitoring; add endpoint row in architecture
- [ ] Smoke: syntax-check bash; pytest capacity tests still pass

---

### Task 4: Commit

- [ ] Commit implementation with a message focused on why (cron pacing signal for Hermes)
