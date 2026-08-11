# Median peer scale for comparable headroom

**Date:** 2026-08-11  
**Status:** approved  
**Supersedes (peer-scale only):** max-based `pool_max_cap` in
`2026-08-11-comparable-headroom-design.md`

## Problem

Cap-scaled comparable headroom used
`remaining / pool_max_cap[W]` with
`pool_max_cap[W] = max(model-scope caps)`. One model-scope group with
unbounded success-streak learning can own every window max, collapsing
peer comparable scores to ~0. `/v1/capacity` then stays on `advice=skip`
and Hermes cron `capacity_gate` skips work even when most free tiers still
have normal remaining room.

## Goal

Keep cap-scaled comparable headroom (binding by local raw `%`, then scale
remaining in that window’s peer units) for routing rank and `/v1/capacity`,
but stop one (or a few) inflated model-scope caps from dominating the peer
denominator.

## Locked decisions

| Topic | Choice |
|-------|--------|
| Scope | Durable code only — no live clear of poisoned groups |
| Learning | Unchanged (AIMD / reclamp / `ensure_fits`) |
| Peer scale | **Median** of active model-scope caps per window key `W` |
| Surfaces | Both `comparable_headroom` (capacity) and `rank_comparable_headroom` |
| Advice bands | Unchanged |
| Dashboard | Raw `%` bars unchanged |

## Scoring

### Peer scale (median)

For each full-grid window key `W` (e.g. `RPM`, `TPM`, … `TPMo`):

```
values = [bucket[W].cap for model-scope groups where bucket[W] is active]
peer_scale[W] = median(values)   # n=0 → 0.0 (raw-% fallback)
```

- Provider-wide groups are **excluded** (same as before).
- Only **active** buckets contribute a cap to the median.
- Even `n`: average of the two central values.
- Small-n: no special max fallback (`n=1` → that single cap).
- Recompute under the limiter lock on each comparable read.

### Per BucketGroup

1. If `blocked_until` in the future → comparable `0.0`.
2. If no active buckets → `1.0`.
3. `binding = argmin_active(tokens/cap)` (unchanged).
4. `comparable = clamp(remaining[binding] / peer_scale[binding], 0, 1)`.
5. If `peer_scale[binding] <= 0` → fall back to raw `tokens/cap`.

Remaining above the median clamps to `1.0` (large healthy tiers look full
until they burn below the typical peer).

### API / wire-up

- Replace `_pool_max_caps_unlocked` with a median peer-scale builder
  (`_pool_peer_caps_unlocked` or equivalent).
- `BucketGroup.comparable_headroom(peer_caps: dict[str, float])` — rename
  the parameter from `pool_max_caps`; behavior otherwise unchanged.
- Public `comparable_headroom` / `rank_comparable_headroom` keep names.
- `rank_comparable_headroom` still `min(PW, model)` against the **same**
  model-scope median peer scale.
- `list_groups()` continues to expose `comparable_headroom` (now
  median-scaled).
- `capacity.py` / `router.py` call sites need no logic change.

```
Active buckets → raw pct = tokens/cap
              → binding = argmin pct
Global model-scope median caps → comparable = remaining / peer_scale[binding]
comparable → routing rank and /v1/capacity
```

## Testing

- Outlier: one huge-cap group + many normal caps → peer scale ≈ normal
  median; a full normal group comparable ≈ 1.0 (not ~0).
- Large low-% still beats tiny high-% when remaining vs median favors the
  large one.
- Binding still by raw `%` (TPM-scale peer must not crush an RPM-bound
  group via the wrong window).
- Even-n median averages the two central caps.
- Empty / missing scale → raw `%` fallback; blocked → 0; missing group → 1.0.
- `rank_comparable_headroom` = min(PW, model) on the same median scale.
- Raw `headroom()` / thin-threshold paths unchanged.

## Out of scope

- Clearing live poisoned TBF state
- Learning ceilings / reclamp changes
- Retuning `/v1/capacity` advice bands
- Dashboard dual-bar UX
- Client proxy budgets (`PROXY_LIMIT_*`)

## Success criteria

With a fleet that includes one absurd-cap model-scope group and many
normal free-tier groups, `/v1/capacity` is not forced to `skip` solely by
that outlier; rank still prefers absolute remaining versus the **typical**
peer (median), not the largest learned fiction.
