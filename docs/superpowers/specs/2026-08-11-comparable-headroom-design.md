# Comparable headroom (cap-scaled binding window)

**Date:** 2026-08-11  
**Status:** approved

## Goal

Routing rank and `/v1/capacity` should prefer **absolute remaining room** on the
**locally binding** rate window, so a large free-tier at low raw `%` can beat a
tiny free-tier at high `%` when it still has more usable room — without treating
RPM and TPM (or minute vs month) as the same physical unit.

## Success criteria

- Ranking uses peer-normalized comparable scores (not raw `tokens/cap` alone).
- `/v1/capacity` feeds the same comparable **model-scope** score into `score_pool`
  (still × health).
- Raw fractional headroom remains for learning, thin-threshold logs, explore
  behavior, and “which window is binding.”
- Large free-tier with more absolute remaining on its binding window outranks a
  smaller tier that only looks fuller as a percentage.
- Cross-type crush avoided: binding window is chosen by local `%`, then scaled
  only in that window’s peer-scale units.

## Decisions

| Topic | Choice |
|-------|--------|
| Preference | Absolute remaining room wins over raw fullness `%` |
| Surfaces | Both routing rank and `/v1/capacity` |
| Normalize | Cap-scaled: `remaining / peer_scale[W]` per window type |
| Peer scale | **Median** of active model-scope caps (see `2026-08-11-median-peer-scale-design.md`; was max) |
| Peer pool | Global TBF inventory of **model-scope** groups only |
| Combine | Local `%` picks binding window; score that window only |
| Raw headroom | Unchanged API; still used for thin/learn/dashboard bars |
| PW in ranking | `min(PW comparable, model comparable)` like today’s `headroom()` |
| Capacity | Model-scope comparable only (authoritative) |

## Why not naive min-across-types

`min_W(remaining_W / global_peer_scale_W)` lets a Gemini-scale TPM peer crush every
small-TPM provider even when that provider is RPM-bound. Binding is therefore
chosen with local `tokens/cap` first; peer scale applies only to that window.

## Scoring

### Peer scale caps

For each full-grid window key `W` (e.g. `RPM`, `TPM`, … `TPMo`):

```
peer_scale[W] = median(bucket[W].cap) over active buckets on model-scope groups
```

Provider-wide groups are **excluded** from the peer scale (their ×10 soft caps would
inflate the scale). Recompute under the limiter lock whenever comparable scores
are read (inventory is small; no separate cache required for correctness).
Peer scale is **median** (not max) so one runaway learned cap cannot collapse
all peers — see `2026-08-11-median-peer-scale-design.md`.

### Per BucketGroup

1. If `blocked_until` in the future → comparable `0.0`.
2. If no active buckets → `1.0`.
3. `binding = argmin_active(tokens/cap)` (same notion as today’s binding bucket).
4. `comparable = clamp(remaining[binding] / peer_scale[binding], 0, 1)`.
5. If `peer_scale[binding] <= 0` → fall back to raw `tokens/cap`.

### AdaptiveRateLimiter API

| Method | Meaning |
|--------|---------|
| `headroom` / `model_headroom` | Unchanged raw fractional headroom |
| `comparable_headroom(provider, key, model)` | Model-scope comparable (capacity) |
| `rank_comparable_headroom(provider, key, model)` | `min(PW, model)` comparable (routing) |

Missing model group → comparable `1.0` (same optimism as today).  
PW-only present for ranking → use PW comparable alone.  
Empty inventory for a window → raw `%` fallback for that binding.

### Call sites

- `router.py` candidate sort: `1.0 - rank_comparable_headroom(...)`.
- `_capacity_candidates()`: `comparable_headroom(...)` into `score_pool`’s
  `headroom` field (field name unchanged; value is comparable).
- Thin-headroom threshold / explore / 429 surprise paths: **keep raw** `%`.

```
Active buckets → raw pct = tokens/cap
              → binding = argmin pct
Global model-scope median caps → comparable = remaining / peer_scale[binding]
comparable → routing rank and /v1/capacity
```

## Wire-up

- Implement `BucketGroup.comparable_headroom(peer_caps: dict[str, float])`.
- `AdaptiveRateLimiter` builds `peer_caps` (median) by scanning model-scope `_groups`
  only (`parse_group_key` / `|model:` present).
- PW comparable uses the **same** model-scope `peer_caps`. PW remaining may
  exceed the model median (×10 priors); clamp to `[0, 1]`.
- Cap learning, header pins, and soft cuts continue to mutate raw caps; the next
  comparable read sees updated peer scales automatically.
- `capacity.py` stays pure; no API change beyond the semantic of `headroom` input.

## Telemetry and UX

- `list_groups()` adds `comparable_headroom` alongside existing raw `headroom`
  and `binding` (null when no active buckets).
- Dashboard rate bars keep **raw** `%` (fraction of own cap). Comparable is
  available via the groups/status JSON for scripts; no required UI redesign.
- `/v1/capacity` `components.headroom` becomes mean of comparable scores (same
  field names). Docs note the semantic shift.
- CSV bucket telemetry keeps raw `headroom` columns (learning/debug).

## Testing

- Unit: binding by raw `%`, then scale by peer median (large low-% beats tiny high-%).
- Unit: TPM-scale peer does not crush an RPM-bound small-TPM group when RPM `%` is
  lower.
- Unit: blocked → 0; missing group → 1.0; zero/empty peer scale → raw fallback.
- Unit: `rank_comparable_headroom` takes min of PW and model.
- Capacity path: candidates use comparable values; advice thresholds unchanged.
- Regression: raw `headroom()` / thin-threshold behavior unchanged.
- Outlier: one huge-cap group must not collapse normal peers’ comparable scores
  (see median-peer-scale design).

## Capacity advice impact

Comparable scores are typically smaller than raw `%` when the binding cap is
below the fleet peer scale. Existing advice bands (`fast`/`normal`/`slow`/`skip`) are
**not** retuned in this work; operators may see more `slow`/`skip` until bands
are revisited later. Median peer scale mitigates perpetual `skip` from a single
runaway max.

## Out of scope

- Request-equivalent conversion (tokens ÷ typical request size)
- Retuning `/v1/capacity` advice thresholds
- Dashboard redesign / dual bars
- Changing TBF learning, soft-cut, or explore-into-limit policy
- Client proxy budgets (`PROXY_LIMIT_*`)
