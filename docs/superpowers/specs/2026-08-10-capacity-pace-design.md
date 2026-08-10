# Capacity pace signal for Hermes cron

**Date:** 2026-08-10  
**Status:** approved

## Goal

Hermes Agent (third-party) cron can ask hermes-router how hard to push right
now and get a stable answer it maps to **stretch cron interval** or **skip this
tick**. hermes-router owns capacity math; Hermes Agent owns cron behavior.

## Success criteria

- `GET /v1/capacity` returns `capacity`, `advice`, `interval_multiplier`,
  `skip`, `reasons`, and `components` under the same access-key auth as other
  `/v1/*` endpoints.
- Score blends authoritative **model-scope** rate headroom with provider health
  (health bucket + circuit breaker), not provider-wide soft estimates alone.
- `hr pace` / `hr pace --json` expose the same signal for scripts.
- Docs describe the Hermes cron contract (skip / stretch / fail-open).
- No Hermes Agent code, dashboard UI, or per-tick batch sizing in this work.

## Decisions

| Topic | Choice |
|-------|--------|
| Split | Hybrid: router score + Hermes maps to interval/skip |
| Levers | Stretch interval + skip tick (not batch shrink) |
| Aggregation | Weighted pool: top-K mean of effective candidate scores |
| API shape | `advice` + `interval_multiplier` + `skip` (+ float `capacity`) |
| Ship | Endpoint + `hr pace` + docs + tests |
| Endpoint | New `GET /v1/capacity` (not nested under `/v1/status`) |

## Architecture

```
Hermes cron tick
    → hr pace | GET /v1/capacity
    → pool capacity score (model headroom × health)
    → advice / interval_multiplier / skip
    → skip? exit : run work, next delay = base × multiplier
```

## Scoring

### Inputs

- Configured `(provider, model)` candidates (chat models from provider config).
- Authoritative **model-scope** rate groups only; missing group → headroom `1.0`.
- Provider `health_bucket` (0/1/2) and breaker open; model `blocked_until`.

### Per candidate

`effective = headroom × health_factor`

| Condition | health_factor |
|-----------|---------------|
| Breaker open or `blocked_until` in the future | `0.0` |
| health_bucket 0 | `1.0` |
| health_bucket 1 | `0.7` |
| health_bucket 2 | `0.3` |

### Pool

- `capacity` = mean of the **top-K** effective scores (`K=3`, or all if fewer).
- If fewer than 2 candidates have `effective > 0.05` → treat as exhausted (`skip`).
- No candidates → `capacity=0`, `advice=skip`, `skip=true`, reason `no_candidates`.

### Advice mapping

| capacity | advice | interval_multiplier | skip |
|----------|--------|---------------------|------|
| ≥ 0.60 | `fast` | `0.5` | false |
| ≥ 0.35 | `normal` | `1.0` | false |
| ≥ 0.15 | `slow` | `2.0` | false |
| < 0.15 | `skip` | `4.0` | true |

## Response contract

```json
{
  "generated_at": 1234567890.0,
  "capacity": 0.42,
  "advice": "normal",
  "interval_multiplier": 1.0,
  "skip": false,
  "reasons": ["top_headroom=0.51", "health_drag=0.09"],
  "components": {
    "headroom": 0.51,
    "health": 0.82,
    "usable_candidates": 5,
    "top_k": 3
  }
}
```

Hermes cron only needs `skip` and `interval_multiplier` (optionally `advice`).

## Surfaces

- `GET /v1/capacity` — read-only; no upstream calls; `_auth_check`.
- `hr pace` — human one-liner; `hr pace --json` — raw body (same auth/base URL
  pattern as `hr status`).
- Docs in monitoring (+ architecture endpoint table).

## Hermes contract (docs only)

1. Tick start: `hr pace --json` or curl `/v1/capacity`.
2. If `skip` → exit without work.
3. Else run; next delay = `base_interval × interval_multiplier`.
4. Endpoint failure → fail-open: treat as `slow` / mult `2.0` / `skip=false`.

## Out of scope

- Hermes Agent implementation
- Dashboard UI for capacity
- Shrinking work per tick
- Env overrides for thresholds (fixed defaults for v1)

## Testing

- Unit tests for score → advice (thresholds, top-K, breaker, blocked_until,
  thin usable set, no candidates).
- HTTP test: `/v1/capacity` auth + response shape.
