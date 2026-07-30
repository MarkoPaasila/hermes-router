# Full-grid multi-window TBF tracking

**Date:** 2026-07-30  
**Status:** Draft for review  
**Approach:** Independent full `[R,T]×[M,H,D,W,Mo]` grid on both scopes; PW tiny soft ticks; soft rank only

## Problem

Hermes already models multi-window token buckets (`RPM`…`TPMo`), but groups usually materialize only a subset (defaults and/or headers). Longer windows can be marked inactive and drop out of debit/rank. That means:

1. Operators cannot see burn across hour/day/week/month for every provider.
2. Provider-wide learning treats a single model 429 like a strong shared-ceiling signal.
3. Thin-headroom hard-skips starve the explore-into-limit path that teaches true quotas—especially on long windows, which rarely get binding feedback.

Minute windows converge naturally (frequent headers/429s). Longer windows need different learning: stable priors, usage accounting always on, cap changes only from real evidence.

## Goals

1. **Always-on full grid:** every provider-wide and every model `BucketGroup` always has all ten buckets: `R`/`T` × `M`/`H`/`D`/`W`/`Mo`.
2. **Init prior:** `Cap(Mo) ≥ Cap(W) ≥ Cap(D) ≥ Cap(H) ≥ Cap(M)` per dimension; explicit defaults/env/auth win; else linear scale from `M`.
3. **Same absolute debit, smaller proportional move** on longer windows (usage and PW soft ticks).
4. **PW:** every model 429 applies a **tiny** soft tick (step shrinks with window length); many 429s accumulate into a clear shared-ceiling signal. One 429 must not yank long PW caps.
5. **Long windows:** no routine success-streak nudge from a cold prior; stay fixed until header (model) or binding/accumulated cut evidence.
6. **Routing:** thin headroom is a **soft preference** only; never hard-skip solely for being near a limit. Admit fails only when a bucket cannot afford the debit.
7. Preserve scope roles: model authoritative (headers, hard 429, Retry-After); provider-wide estimate (no headers, no Retry-After).

## Non-goals

- Tracking-only / inactive long windows (rejected in favor of always-binding full grid).
- Evidence-gated PW cuts that skip the first N 429s (accumulate tiny ticks instead).
- Derived-only long windows that are pure `f(M)` with no independent ledger (cannot express tight daily quotas vs loose RPM without an explicit row).
- Synthetic probe traffic to discover long-window caps.
- Changing AND-gate consume (pw + model) or credential rotation.
- New provider-specific header parsers beyond the existing map.

## Architecture

Dual-scope shape unchanged:

```
BucketGroup (provider_wide | model): always 10 TokenBuckets
  RPM RPH RPD RPW RPMo
  TPM TPH TPD TPW TPMo

Request
  → rank by soft headroom (min across buckets / scopes); no thin hard-skip
  → check_and_consume(pw, mg)   # full debit all 10 on each scope
  → upstream
  → success: reconcile; success nudges on M (and existing PW M knobs only)
  → 429: model hard binding cut (+ headers, Retry-After);
         pw tiny soft tick on each bucket (no headers, no Retry-After);
         optional surprise = extra tiny-tick family on pw when model was high-headroom
```

### Init

For each dimension `R` and `T`:

1. Start from known `M` cap (`PROVIDER_RATE_DEFAULTS`, `RATE_DEFAULT_*`, `auth.json` `rate_defaults`).
2. For each longer window missing an explicit value:  
   `Cap(W) = Cap(M) × (WINDOWS[W] / WINDOWS[M])`.
3. If an explicit H/D/W/Mo value exists (table/env/auth), use it (overrides scale).
4. After fill, enforce ordering `Mo ≥ W ≥ D ≥ H ≥ M`. **Explicit** quotas are sticky: if an explicit daily (etc.) is below a purely scaled shorter window, lower the scaled siblings to restore order—do not raise the explicit quota to match linear scale.
5. Provider-wide new groups: apply existing `RATE_PROVIDER_CAP_MULTIPLIER` (×10) to the **base** caps after step 1–4, without re-multiplying on state load.

### Consume and headroom

- Every request debits **all ten** buckets on both scopes (request count into `R*`, tokens into `T*`).
- `headroom()` for a group remains `min(bucket.headroom)` over the full grid (and `0` while model `blocked_until` applies).
- Ranking uses that headroom as a soft score among eligible candidates.
- Remove thin-headroom **hard-skip** driven only by `RATE_HEADROOM_THRESHOLD` (or equivalent). Threshold may remain as a ranking weight input if useful; it must not veto a candidate that can still afford the debit.
- `ensure_fits` / request-burst lift unchanged per bucket that must admit the debit.

### Inactive / persistence

- Remove or no-op `check_inactive` auto-deactivation of long windows.
- Persist the full grid in `rate_limits_state.json`.
- On load: restore present buckets; **backfill** any missing limit keys via current init rules; do **not** re-apply ×10 to already-persisted provider-wide caps.

## Learning rules

### Model

| Event | Behavior |
|-------|----------|
| Headers | Pin matching buckets only |
| 429 | Hard cut on binding window(s) (low headroom / near-tie); headers; Retry-After |
| Success streak | Nudge **M** (RPM/TPM) as today; **no** routine cold nudge for H/D/W/Mo |
| Long-window raise | Header pin, or later policy only after binding evidence—not “never emptied” |

### Provider-wide

| Event | Behavior |
|-------|----------|
| Headers | Never applied |
| Retry-After | Never applied |
| Each model 429 | Tiny soft tick on each PW bucket: fractional step shrinks with window length, e.g. `cap *= 1 - ε × (T_M / T_window)`, subject to existing soft floor |
| Surprise path | Keep as optional extra tick(s) in the same tiny-tick family when pre-attempt model headroom was very high; still bounded |
| Success streak | Existing PW nudge knobs apply to **M** only in this design; long PW caps stay sticky |

Absolute spend and absolute tick magnitudes may be comparable across windows; **as a fraction of cap**, longer windows must move less. Repeated 429s accumulate until long PW caps clearly reflect a shared ceiling.

### Why long windows converge

- **Down:** model — hard cut when that window is binding; PW — accumulated tiny ticks on every 429 (long windows move slowly by construction).
- **Up:** not from idle headroom. Only headers (model) or healing after an overly aggressive **M** cut via success nudges. Exploring near the limit (soft rank, no hard-skip) is intentional so true ceilings can be hit and learned.

## Edge cases

- **Single model:** identical absolute debit; PW % still diverges (×10 prior + smaller ticks).
- **Explicit tight quota (e.g. Gemini RPD):** table value wins over linear scale; may dominate `min(headroom)` ranking without blocking admit until true exhaust.
- **Header for one window:** pin that model bucket; siblings untouched; PW untouched.
- **Oversized request:** existing burst lift per constraining bucket.
- **Legacy state:** subset of buckets → backfill missing keys; preserve learned caps.
- **Surprise false positive:** soft floor + tiny steps; M nudges heal; long PW remains sticky.

## Testing

1. New pw + model groups each expose all 10 limit keys.
2. Missing H/D/W/Mo filled by `Cap(M)×(T/T_M)`; explicit RPD (etc.) overrides scale; ordering prior holds.
3. One consume: all 10 debit; Mo headroom % drops less than M.
4. One PW soft tick: M moves more fractionally than Mo.
5. Many PW ticks: Mo eventually moves materially.
6. Thin headroom lowers rank but does not hard-skip; admit succeeds until true exhaust.
7. No routine success nudge on cold H/D/W/Mo.
8. State load backfills missing windows; persisted PW caps not re-×10’d.
9. Model hard 429 still binding-selective; Retry-After model-only; PW gets ticks without headers/holds.

## Docs

Update configuration / architecture / monitoring notes: always-on full grid; PW tiny ticks; soft rank only; long windows evidence-only for upward movement.

## Success criteria

- Dashboard/API always show `[R,T]×[M,H,D,W,Mo]` for provider-wide and model rows.
- A single 429 barely moves PW long-window caps; sustained 429s clearly do.
- Near-limit providers remain eligible; real exhaust/429 still teaches caps.
- Minute windows remain the primary fast learner; longer windows stay stable priors until evidence.
