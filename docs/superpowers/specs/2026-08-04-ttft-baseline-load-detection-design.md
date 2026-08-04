# TTFT baseline load detection

**Date:** 2026-08-04  
**Status:** approved

## Goal

Detect when a catalog candidate is under unusual load by comparing
**time-to-first-byte (TTFT)** to a per-candidate statistical baseline, then
**abort that attempt early**, clear **session affinity** when the aborted
candidate was affine, and **cascade** to the next candidate on the same
request — without lasting cooldowns or catalog demotion.

## Terminology

| Term | Meaning |
|------|---------|
| **TTFT** | Wall time from starting the upstream HTTP call until the first response body byte that shows the body has begun (streaming: first SSE / `data:` chunk or equivalent; non-streaming: first body bytes once headers are complete). |
| **TTFT baseline** | Per-candidate typical successful TTFT (EWMA), plus sample count. |
| **TTFT deadline** | Per-attempt wait bound for first byte: cold absolute, or `max(floor, mult × ewma)` when warm. |
| **TTFT abort** | Cancel the in-flight upstream call because the TTFT deadline elapsed with no first byte. Load/capacity signal — not auth, payload, unsuitable-model, or breaker failure. |

**Aligns with existing glossary:** Fallback / Cascade / Session affinity / Candidate
(`CONTEXT.md`). Prefer those names in user-facing docs over “stickiness.”

## Context (current behavior)

- Session affinity reuses `(provider, model, key)` until hard fallback
  (rate-limit, errors, breaker, caps, unsuitable cooling, etc.).
- `ProviderStats` records lifetime average latency for observability and
  breaker health; it does **not** break affinity on “just slow.”
- Upstream read timeouts are long (on the order of 120–180s), so a loaded
  affine candidate can stall the client for a long time before cascade.

## Decision summary

1. Signal: **TTFT only** (not total duration, not tokens/sec).
2. Action: **abort + cascade on the same request**; clear affinity if the
   aborted candidate was the session’s affine candidate.
3. Scope: **every cascade attempt**, each with its own candidate deadline.
4. Grain: baseline per **`(provider, model)`**.
5. Threshold: **absolute floor + relative** to EWMA when warm.
6. Cold start: **higher absolute-only** deadline until enough samples.
7. Aftermath: **no shared cooldown / ranking demotion**; only baselines update.
8. Learning: record TTFT **only** when first byte arrives; **never** from aborts.

## Architecture

```
cascade attempt
    → TtftBaselineStore.deadline_s(provider, model)
    → start upstream with wait-for-first-byte = deadline
    → first byte? ──yes──→ record(ttft) → continue under normal read timeout
                 └──no───→ TTFT abort → release rate reservation
                           → leave sticky if affine
                           → cascade note ttft_deadline
                           → next candidate
```

- New store sits beside session sticky / rate limiter; cascade owns abort +
  fallback.
- Circuit breaker / `record_health` are **not** tripped by TTFT abort
  (load ≠ provider down).
- Unsuitable-model cooling and provider-wide skip lists are unchanged.

## Components

### `TtftBaselineStore`

In-memory, thread-safe, keyed by `(provider, model)`.

Per entry (minimum):

- `ewma_s` — exponential moving average of successful TTFT seconds
- `sample_count` — number of successful TTFT samples
- `last_ttft_s` — most recent successful sample (debug / dashboard)

API:

- `deadline_s(provider, model) -> float`
- `record(provider, model, ttft_s) -> None`
- `summary(provider, model) -> dict` (optional; for status / dashboard)

**Not persisted** across restarts. After restart every candidate is cold until
it accumulates samples again.

EWMA update (successful first byte only):

```
α = TTFT_EWMA_ALPHA   # default 0.2
ewma = ttft_s  if sample_count == 0 else (α * ttft_s + (1 - α) * ewma)
sample_count += 1
```

### Deadline formula

| State | Condition | Deadline |
|-------|-----------|----------|
| Cold | `sample_count < TTFT_MIN_SAMPLES` | `TTFT_COLD_DEADLINE_S` |
| Warm | otherwise | `max(TTFT_FLOOR_S, TTFT_MULT × ewma_s)` |

Default knobs (env-overridable):

| Knob | Default | Role |
|------|---------|------|
| `TTFT_FLOOR_S` | `3.0` | Never early-abort below this wait when warm |
| `TTFT_MULT` | `3.0` | Unusual = this multiple of typical TTFT |
| `TTFT_MIN_SAMPLES` | `5` | Samples before relative deadline |
| `TTFT_COLD_DEADLINE_S` | `20.0` | Absolute wait while cold |
| `TTFT_EWMA_ALPHA` | `0.2` | EWMA smoothing |
| `TTFT_ABORT_ENABLED` | `1` | Feature flag (`0` = no early abort; successful TTFT is still recorded so enabling later starts warmer) |

After first byte, the existing long read timeout remains in force for the rest
of the completion / stream.

### Measurement

- Start clock when the upstream request is initiated (same `t0` used for
  attempt latency today, or an adjacent mark immediately before send).
- Streaming: TTFT = time until first body chunk that indicates the stream has
  started (first SSE `data:` line or first non-empty chunk from the streaming
  iterator — implementation picks the earliest reliable signal already exposed
  by the HTTP stack).
- Non-streaming: TTFT = time until response headers are complete **and** the
  first body byte is available (or headers-complete if the client cannot expose
  body start separately without buffering the whole response — prefer true
  first-byte when practical).
- Connect / DNS / TLS failures stay on the existing **network** failure path;
  they do not call `record()` and are not labeled `ttft_deadline`.

### Cascade & session affinity

On TTFT abort:

1. Cancel / close the upstream connection.
2. Release the rate-limiter reservation (same as network failure).
3. Cascade trail: failed/skipped note with reason **`ttft_deadline`** (include
   waited seconds and baseline deadline in the log line).
4. If session affinity pointed at this `(provider, model)`, clear it
   (`_leave_sticky_model`).
5. Continue the cascade with the next candidate (that candidate gets its own
   deadline).
6. Do **not** add the provider to `skip_providers`, do **not**
   `record_health(False)`, do **not** unsuitable-cool the model.
7. Do **not** update EWMA.

On success after first byte: existing sticky remember behavior unchanged.

If every candidate fails (including TTFT aborts), existing exhausted / 503
behavior stands.

## Observability

- Request log / cascade trail: `ttft_deadline` reason.
- Log line example: `TTFT abort name/model after 9.2s (deadline 8.1s, ewma 2.7s, n=12)`.
- Optional later: expose per-candidate `ttft_ewma_ms` / `ttft_samples` on status
  or Models UI — not required for the first cut if cascade + logs are enough.

## Testing

- Cold deadline returns `TTFT_COLD_DEADLINE_S` until `TTFT_MIN_SAMPLES`.
- Warm deadline is `max(floor, mult × ewma)`.
- `record` updates EWMA; abort path never calls `record`.
- Cascade: TTFT abort clears sticky when affine and tries the next candidate.
- Cascade: TTFT abort does not trip breaker / unsuitable / provider skip.
- Streaming and non-streaming measurement hooks covered with stubs / fakes
  (no live provider load tests in CI).
- Feature flag off → no early abort (ordinary upstream timeout only).

## Non-goals

- Hedged / parallel secondary attempts
- Shared cooldown or soft catalog demotion after abort
- Percentile / MAD windows (revisit only if EWMA proves too noisy)
- Persisting baselines across restarts
- Total request duration or tokens/sec as the abort signal
- Tripping the circuit breaker on TTFT abort

## Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Death spiral (aborts teach a lower baseline) | Never `record` on abort |
| Aborting healthy slow first tokens (reasoning models) | Floor + mult; cold absolute; tune via env |
| Streaming TTFT hard to pin | Prefer first SSE/data chunk; document the chosen hook |
| Free-tier / cold models always hit cold absolute | Expected; relative kicks in after `N` successes |

## Out of scope for follow-ups (unless revisited)

- Key-level baselines `(provider, model, key)`
- Persisted / borrowed sibling priors
- Dashboard charts beyond optional summary fields
