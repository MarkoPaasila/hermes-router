# Equal-size GI complexity buckets

**Date:** 2026-08-21  
**Status:** approved

## Problem

Complexity 1–5 maps to fixed minimum GI bars `0 / 20 / 40 / 60 / 80`. Those cut points are equal on the **score axis**, not equal in **how many catalog candidates** sit above each bar. Dense or sparse regions of the live catalog skew which models qualify for a given complexity.

## Goals

- Keep five complexity bands and the existing “cheapest candidate that clears min GI” selection shape.
- Derive min-GI cut points so bands have approximately equal `(provider, model)` headcount in **this** router’s catalog.
- Refresh cut points whenever that catalog or resolved GI scores change.
- Expose live mins for operators (log + `/v1/status`).

## Non-goals

- Changing how request complexity is classified.
- Changing GI snapshot sources, aliases, or override UX.
- Changing selection sort keys beyond the source of the min-GI bar.
- Falling back to the hard-coded `0/20/40/60/80` ladder for small catalogs.

## Population

- Every `(provider, model)` chat candidate from `PROVIDERS` (same expansion as `_get_smart_ordered`: each entry in `provider["models"]`, or `[provider["model"]]` if absent).
- Include models that resolve to GI `0` by default (no snapshot/override).
- Count each provider–model pair separately (same model on two providers counts twice).

## Algorithm

1. Collect resolved GI scores for the population; sort ascending.
2. Complexity `1` → min GI `0`.
3. Complexity `c ∈ {2,3,4,5}` → nearest-rank percentile at `(c − 1) × 20` (20 / 40 / 60 / 80).
4. Empty catalog → all five mins are `0`.
5. Tiny catalogs use the same rule; bars may coincide. No fixed-ladder fallback.

Nearest-rank: for percentile `p` and `n` scores, index `ceil(p/100 × n) − 1`, clamped to `[0, n − 1]`.

## Ownership and refresh

- `gi_ranking` caches `{1..5 → min_gi}`, exposes `recompute_complexity_thresholds(scores: list[float])` and `min_gi_for_complexity(c)`.
- Router collects scores and calls recompute when a dirty flag is set.
- Mark dirty when: provider `models` lists change (discovery, catalog restore, exclude/filter that alters the list); GI override set/clear; snapshot or overrides file hot-reload (mtime).
- Before reading mins in `_get_smart_ordered` (and when building status), recompute if dirty.
- Startup: dirty until the first refresh; empty list keeps mins at `0`.

Fixed `COMPLEXITY_MIN_GI` is not the live source of truth.

## Observability

- Info log on recompute: score count `n` and the five mins.
- `/v1/status` includes the current complexity→min-GI map.

## Testing

- Unit: empty → all `0`; evenly spaced scores; ties/duplicates; tiny `n`; complexity `1` always `0`.
- Update tests that hard-code mins `40` / `80` to recompute from a fixture catalog first.
- Dirty → catalog/override change → next ordering uses new bars.

## Docs

- ADR-0002 and routing docs: complexity maps to **catalog-relative percentile** mins, not fixed `0/20/40/60/80`.
