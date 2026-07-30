# Session-Sticky Catalog Routing (TBF-Compatible Balancing)

**Date:** 2026-07-30  
**Status:** Approved for implementation planning  
**Approach:** Session-sticky full-catalog selection until cascade-away; router-only session ids

## Problem

After switching rate pacing to adaptive token-bucket filters (TBF), several pre-TBF balancing leftovers remain:

- Provider-level round-robin (`_rr_counter`, `ROTATION_MODE` round-robin/sequential) spreads load in ways that dilute per-bucket learning and no longer match “drain until rate-limit” semantics (429s are TBF-owned, not key `cool_until`).
- Dashboard splits provider health and TBF across Providers vs a standalone Rate limits page; operator mental model is “provider health includes headroom.”
- `PROVIDER_MODEL_DEFAULT` / Reset-to-default is stale display fiction once catalogs are discovery-driven.

We need load-balancing that **converges TBF well**: keep traffic on one ledger until a real limit/failure, then re-pick the best catalog candidate with enough headroom.

## Goals

1. Remove provider round-robin; select from the **full chat catalog** of `(provider, model)` candidates.
2. **Sticky until pressure:** within a conversation, reuse `(provider, model, key)` until that candidate is cascaded away; then pick the best remaining model with enough headroom.
3. Session identity is **router-only**: honor client-supplied ids when present; without an id, pick fresh each request (no invented sticky key). Hermes Agent does not yet forward `session_id` to custom endpoints — out of scope to patch Hermes in this change.
4. Move TBF UI: **Providers** = health + provider-wide TBF; **Models** = capabilities + model-scope TBF; remove standalone Rate limits nav/page.
5. Remove default-model table and Reset-to-default UX; catalogs stay dynamic (discovery + explicit env overrides).
6. Retire `ROTATION_MODE`; keys are sticky-until-fail within the sticky model.

## Non-goals

- Patching Hermes Agent to always send `X-Hermes-Session-Id` (follow-up).
- Soft headroom-bias stickiness (always re-score with a sticky bonus).
- Changing TBF ledger semantics (provider-wide estimate vs model authoritative, dual debit, surprise soft cut, header pins).
- Session-sticky embeddings (keep current embed key/TBF behavior).
- Synthetic probe traffic to grow caps.

## Why sticky-until-failure for TBF

| Policy | Effect on TBF convergence |
|--------|---------------------------|
| Provider / model round-robin | Spreads samples across many buckets; slow, noisy learning |
| Preemptive thin-headroom switch | Avoids hitting limits; fewer hard 429 / header calibration events |
| **Sticky until cascade-away** | One `(key, model)` ledger absorbs traffic until a real failure → strongest learning signal |

Pressure is defined as **any non-success that already causes the router to cascade away** from that `(provider, model)` (429, TBF admit exhaustion after short wait, no ready keys, 5xx/timeout after key retries, breaker, etc.). Thin headroom alone does **not** break stickiness for session traffic.

## Architecture

```
Request
  → resolve session_id (or None)
  → rank full catalog (capability/price/quality, then health/breaker/headroom)
  → if sticky valid: prefer that (provider, model) first; sticky key if ready
  → TBF check_and_consume → upstream
  → success: set/update sticky (provider, model, key)
  → cascade-away from model: clear sticky; continue ranked failover
  → success on failover: write new sticky
```

TBF dual ledgers and ranking headroom peek remain; provider `_rr_counter` rotation is removed.

## Components

### 1. `SessionStickyStore`

Thread-safe in-memory map:

`session_id → {provider, model, key, updated_at}`

- API: `get(session_id)`, `set(...)`, `clear(session_id)`
- Idle TTL **3600s** and hard cap **10_000** entries (evict oldest `updated_at` when over cap)
- Not persisted across process restart (acceptable; clients re-stick on next success)

### 2. `_resolve_session_id(request, body)`

Priority (first non-empty wins):

1. Header `X-Hermes-Session-Id`
2. Header `X-Chat-ID`
3. Body field `user` (OpenAI end-user id)
4. Body `metadata.session_id` or `metadata.sessionId`

Missing → `None` → no stickiness for that request.

**Note:** Hermes Agent maintains an internal `session_id` but currently forwards it outbound only for xAI (`x-grok-conv-id`), Qwen (`metadata.sessionId`), and Codex (`prompt_cache_key`). Custom hermes-router calls do not send it today.

### 3. Catalog ordering (`_get_smart_ordered` → catalog order)

- Flatten all configured chat `(provider, model)` candidates (discovery + env lists).
- **Remove** provider-list rotation via `_rr_counter`.
- Sort key unchanged in spirit: local/fast profile, capability tier, price, quality, breaker, health, rate headroom, availability, list index.
- When sticky exists and still in catalog, place that candidate first (failover order = rest of ranked list).

### 4. `KeyPool`

- Remove `ROTATION_MODE` (`round-robin` / `sequential`) selection modes and dashboard control.
- New behavior: prefer sticky key if present and not cooling; else first ready key for that model.
- `cool_until` / `mark_key_down` remain **health-only** (network/5xx), not upstream rate limits.
- Old `ROTATION_MODE` env value: ignore with a single startup log line (no crash).

### 5. Dashboard & config cleanup

| Remove | Destination / replacement |
|--------|---------------------------|
| Rate limits nav + page | Providers: provider-wide TBF + health; Models: model-scope TBF + capabilities |
| `PROVIDER_MODEL_DEFAULT`, default hint, Reset-to-default | Keep **Save model** (writes `{PROVIDER}_MODEL` to `.env`); remove built-in defaults table and Reset button/API that restores code defaults |
| Rotation mode selector | Sticky-key-until-fail (documented) |

`/v1/rate-limits` API remains as the data source; UI filters by `scope` (`provider_wide` vs `model`). Clear-group and orphan toggle live on the Models page (model rows) and Providers page (provider-wide rows) as needed.

### 6. Docs

Update routing / architecture / configuration / monitoring: sticky-until-failure, session id resolution, no provider RR, TBF UI locations, removal of rotation mode and default-model reset.

## Data flow

### With session id

1. Resolve id → `get` sticky.
2. Build ranked catalog; sticky `(provider, model)` first if still configured.
3. For sticky model: use sticky key if ready; else next ready key; on success update sticky key.
4. On success: `set` sticky to winning `(provider, model, key)`.
5. On cascade-away from that model: `clear` sticky (after key retries for that model are exhausted, matching today’s inner loop). Continue failover; first success re-`set`s sticky.

### Without session id

Same catalog ranking and TBF/failover every request; never read/write sticky store.

### Embeddings

Unchanged; no session sticky in this design.

## Error handling & edge cases

- Sticky model removed from catalog / no keys → clear sticky, fresh catalog pick.
- Health cooling on sticky key → try other keys on same model; update sticky key on success; if none ready, cascade model and clear sticky.
- TBF admit fail / 429 → existing TBF paths; clear sticky when leaving the model.
- Concurrent same-session requests → map updates under lock; last successful write wins; do not hold lock across upstream I/O.
- Do not invent session ids.
- Stale `.env` `ROTATION_MODE` / unused default-model helpers → ignored safely.

## Testing

1. Session resolution priority and missing → `None`.
2. Sticky happy path: same session + successes → same `(provider, model, key)`.
3. Cascade clears sticky: after cascade-away, next same-session request stores a new winner.
4. No session: requests do not share sticky state.
5. Key sticky + `mark_key_down` → next key, sticky key updated.
6. Gone sticky target → cleared, fresh pick.
7. TTL (3600s) / hard-cap (10_000) eviction.
8. TBF consume / 429 / reconcile regressions unchanged.
9. Dashboard: no Rate limits page; provider-wide vs model scope rendered on the correct pages; no default-model Reset; rotation mode control gone.

## Success criteria

- No provider round-robin in chat candidate ordering.
- With a session id, traffic sticks to one `(provider, model, key)` until cascade-away, then re-picks from the full catalog.
- Without a session id, behavior is fresh full-catalog selection each request.
- Operators see provider health + provider-wide TBF on Providers, model TBF on Models; standalone Rate limits page removed.
- Default-model table / Reset-to-default gone; `ROTATION_MODE` retired.
- TBF learning still receives real 429/header signals from sticky traffic (no preemptive sticky break on thin headroom alone).

## Implementation notes (for planning)

- Prefer a small `SessionStickyStore` class over scattering dict+lock in the send loop.
- Avoid calling `KeyPool.get_key` for headroom *peek* during ranking if that advances selection side effects; use a read-only peek or headroom without rotating keys.
- Keep YAGNI: no Redis, no sticky persistence, no Hermes Agent patch in this slice.
