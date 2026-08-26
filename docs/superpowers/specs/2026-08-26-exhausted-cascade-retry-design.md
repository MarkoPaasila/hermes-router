# Exhausted cascade retry (keys + breaker)

**Date:** 2026-08-26  
**Status:** Approved for implementation

## Problem

Auto cascade (`hermes-router`) can return **503 All providers exhausted** while models
still work when tried later (including via pin):

- **`keys_cooling`**: all keys for a `(provider, model)` are mid health cool-down
  (`mark_key_down` after network/5xx). There is already a wait-and-retry for short
  **`rate_hold`** waits (`RATE_EXHAUSTED_WAIT_S`), but not for key cool-downs.
- **`circuit_open`**: open breakers are skipped whenever any other candidate looks
  healthy. After that healthy set fails, open providers never get a second chance.
  Pinning a lone model often probes the open breaker (`any_closed` is false when every
  remaining candidate is open), so the same provider succeeds.

## Goals

- After a full cascade failure, allow **one** exhausted retry that:
  - waits up to `RATE_EXHAUSTED_WAIT_S` for the shortest remaining `rate_hold` or
    `keys_cooling` ready-in, then re-walks the catalog; and/or
  - probes **circuit-open** providers on that second pass (even with no sleep).
- Preserve the cascade trail across the retry.
- Apply the same behavior to **embeddings**.

## Non-goals

- Multi-round “requirement ladders” beyond one retry.
- Relaxing `token_cap`, `unsuitable_cooling`, tools/vision filters.
- Force-using keys that are still mid-cooldown (waiting clears them).
- New env knobs (reuse `RATE_EXHAUSTED_WAIT_S`).

## Behavior

After the first cascade pass with no success:

1. Collect the shortest positive wait among tracked `rate_hold` waits and
   `keys_cooling` `pool.ready_in(...)` values.
2. If `0 < best_wait <= RATE_EXHAUSTED_WAIT_S` (default 60s): sleep that long, then
   run one second pass.
3. If any **`circuit_open`** was skipped on the first pass: run the second pass even
   when there is no sleep (or the wait was above the cap)—breaker probe only.
4. Second pass: force breaker probing (do not skip open breakers). Other skips
   unchanged.
5. Exactly one exhausted retry. If still exhausted → same 503 as today.

## Integration

- `_route_completion`: generalize `_rate_retry` → `_exhausted_retry`; track
  `_best_key_wait` and whether any `circuit_open` was skipped; unify the exhausted
  decision; on retry set breaker probing on.
- Embeddings cascade: same wait tracking + one exhausted retry with breaker probe.
- Cascade trail already preserved when `_rate_retry` is true; keep that for the
  renamed flag.
- Log one info line when entering the retry (`wait=…s` and/or `breaker_probe`).

## Testing

- Keys cooling with short ready-in → mocked sleep → second pass success.
- Circuit-open skipped while others fail → no-sleep second pass probes and succeeds.
- Keys wait above cap and no circuit skips → 503, no retry.
- Existing rate_hold exhausted-retry still works.
- Embeddings: breaker-probe-on-retry succeeds.

## Docs

- Routing / architecture fallback: note the one exhausted wait-and-retry including
  key cool-downs and circuit-open probes.
