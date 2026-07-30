# Provider vs model TBF headroom divergence

**Date:** 2026-07-30  
**Status:** Draft for review  
**Approach:** Scaled provider defaults (×10) + full dual debit + surprise soft cut; no probes

## Problem

Provider-wide and model rate-limit ledgers start from the same default caps and receive the same absolute debit on every request. With a single active model (and no provider header sync), dashboard headroom bars move in lockstep. That hides the intended split: model ledgers are per-model and authoritative; provider-wide is a shared key-ceiling **estimate**.

## Goals

1. Model and provider-wide headroom must not be twin bars, even when only one model has traffic.
2. Provider-wide remains a true shared account ceiling: **full** debit of real request/token spend on every request (and on reconcile).
3. Provider ceiling starts generous (**×10** model defaults) and can **converge** via soft 429s, the surprise cut, and success nudges.
4. **Both** model and provider caps must be able to grow past defaults and past post-429 cuts via success-streak nudges after natural refill — never via synthetic probe traffic.
5. Model ledgers stay authoritative (headers + hard 429 + Retry-After). Provider-wide stays estimate-only (no header overwrite, no Retry-After hold on the shared ledger).

## Non-goals

- Synthetic probe / half-open requests to “test” higher caps (too expensive on tight RPM, e.g. Gemini).
- Fractional provider debits (1/N of usage) as the primary divergence mechanism.
- Syncing `x-ratelimit-*` onto provider-wide.
- New per-provider explicit ceiling env vars in the first iteration (may be added later).

## Architecture

Shape unchanged: one `BucketGroup` per `(provider, key)` (provider-wide) and one per `(provider, key, model)` (model). Ranking still uses `min(provider_wide, model)` headroom.

```
Request
  → check_and_consume(pw, mg)     # same absolute debit both
  → upstream
  → success: reconcile(actual) on both; on_success nudges (model + provider knobs)
  → 429: mg hard on_429 (+ headers, Retry-After);
         pw soft on_429 (no headers, no Retry-After);
         if model headroom_before ≥ 0.9 → one extra bounded soft cut on pw
```

Natural refill restores tokens toward the **current** cap. Thin-headroom skips remain; growth happens when real demand succeeds after refill, not from invented probes.

## Behavior

### Init

- **Model groups:** existing `PROVIDER_RATE_DEFAULTS`, `RATE_DEFAULT_*` env, and `auth.json` `rate_defaults` (unchanged).
- **Provider-wide groups (new only):** for each limit key present in the model/base defaults, set  
  `provider_cap = base_cap * 10`  
  (fixed multiplier; starting prior, not a hard maximum).
- **Persistence:** loading `rate_limits_state.json` must **not** re-apply ×10 to already-learned provider caps. ×10 applies only when creating a new provider-wide group (first traffic or after Clear on that row).

### Debit and reconcile

- Unchanged dual full debit of `req_count` / `token_count` on provider-wide and model.
- Reconcile reserved → actual on both scopes (including streaming), as already implemented.

### 429 paths

| Scope | Cut | Headers | Retry-After |
|-------|-----|---------|-------------|
| Model | Hard (existing) | Yes | Yes (model only) |
| Provider-wide | Soft (existing) | No | No |

**Surprise heuristic:** if the model ledger’s headroom was ≥ **0.9** immediately before the attempt that received 429, apply **one additional** soft cut on provider-wide (on top of the normal soft provider `on_429`). Bound: at most **one** surprise soft cut per provider-wide group per rolling **60s** wall-clock window.

### Growth (both scopes)

- Model: existing `RATE_LEARN_SUCCESS_STREAK` / `RATE_LEARN_NUDGE_PCT` (and related) raise caps after success streaks.
- Provider-wide: existing `RATE_LEARN_SUCCESS_STREAK_PROVIDER` / `RATE_LEARN_NUDGE_PCT_PROVIDER` raise caps after success streaks.
- Caps may exceed the original defaults and the ×10 prior; cuts are not permanent ceilings.
- **No probe path.** Empty headroom waits for refill; routing uses normal candidate selection only.

### Dashboard / API

- Two rows remain (provider-wide estimate vs model authoritative).
- After this change, the same spend should reduce model % more than provider % until learning moves caps (≈10× with fresh defaults).
- Binding (RPM/TPM/…) stays per-row independently.

## Edge cases

- **Single model:** absolute spend identical; % headroom diverges because provider caps start 10× larger.
- **Multiple models:** provider tracks sum of spend; each model row stays independent.
- **Auth/env overrides:** base caps for the provider feed both; provider-wide multiplies that base by 10 at creation.
- **Surprise false positive:** bounded extra soft cut; success nudges heal over time.
- **Tight RPM (Gemini):** no probes; skip-on-thin until refill.

## Testing

1. New provider-wide group: each cap equals 10 × corresponding model/base default for that provider.
2. Single consume of fixed tokens: model headroom decreases more than provider-wide headroom.
3. 429 with pre-attempt model headroom ≥ 0.9: surprise path applies extra provider soft cut.
4. 429 with low model headroom: normal soft provider cut only (no surprise).
5. Load from state: persisted provider caps are not re-multiplied by 10.
6. Success streaks can raise model and provider caps above post-cut values.

## Docs

Update configuration / architecture notes: provider-wide is a shared-ceiling estimate with a ×10 prior; model is authoritative; both can grow via success nudges after refill; twin bars are not expected after this change.

## Success criteria

- Fresh OpenRouter/OpenCode (or similar) traffic no longer shows identical provider-wide and model headroom percentages after one request.
- Provider and model still both debit real usage (shared-ceiling accounting preserved).
- After 429s and later successes, caps can move down and back up without requiring probe requests.
- Existing model header sync and Retry-After behavior unchanged.
