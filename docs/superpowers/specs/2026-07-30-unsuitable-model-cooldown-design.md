# Unsuitable-model cooldown + specialized discovery filter

**Date:** 2026-07-30  
**Status:** Approved for implementation

## Problem

Large auto-discovered Gemini catalogs flood chat routing with non-chat models (`imagen-*`,
`*-native-audio-*`, embeddings, `deep-research-*`, etc.). Each request pays many 404/400
round-trips before a usable model wins. Today a 400/404 only skips that model for the
**current** request — nothing is remembered.

## Goals

- Cut chat cascades caused by non-chat / missing models.
- Remember **model-unsuitable** failures across requests with **exponential backoff**.
- Keep payload-shaped 400s as one-shot cascades (no denylist).
- Do **not** persist cooldowns across restarts, strip `{PROVIDER}_MODEL`, or change
  429 / TBF / auth / 413 behavior.

## Non-goals

- Disk persistence of cooldowns (follow-up if restarts re-flood).
- Auto-filtering configured `{PROVIDER}_MODEL` lists.
- Request-log cascade reason fields (observability follow-up).
- Dashboard UI for cooled models.

## Catalog half (discovery only)

`FILTER_SPECIALIZED_MODELS` stays opt-in. When enabled it continues to drop specialized
IDs from **auto-discovered extras only**. Configured models are never stripped.

Expand `_SPECIALIZED_NAME_PATTERNS` / metadata tokens with at least:
`deep-research`, `robotics`, `lyria`, `nano-banana`, `aqa` (existing `imagen`, `audio`,
`embed`, … stay).

`{PROVIDER}_EXCLUDE_MODELS` remains the manual escape hatch for configured junk
(e.g. `gemini-embedding-2` in `GEMINI_MODEL`).

Docs note: with a large `AUTO_DISCOVER_MODEL_LIMIT`, enable `filter_specialized_models`
to avoid flooding the chat roster.

## Runtime half — unsuitable-model cooldown

In-memory map keyed by `(provider, model)`.

| Event | Behavior |
|---|---|
| **404** | Always unsuitable → record failure, break to next candidate |
| **400** body matches model-unsuitable cues (`model not found`, `not supported`, `unknown model`, similar) | Same as 404 |
| **Other 400** (payload / reasoning replay / schema) | Cascade only — no cooldown |
| **429** | Unchanged — `rate_limiter.on_429` / TBF only |
| **Success** on that model | Clear failure streak |

**Backoff:** `delay = min(cap, base * 2^(failures-1))` with defaults `base=60s`,
`cap=1h`. While `now < cool_until`, skip in `_route_completion` (no `forward`, no
attempt increment). After expiry, one probe is allowed; success clears, failure
doubles again. Restart resets.

## Integration

- Hook in `_route_completion`: skip cooling candidates; on unsuitable 404/400 call
  `record_unsuitable`; on success call `clear_unsuitable`.
- Pure classifier over status + body for unit tests.
- Log skip/cool with failure count + ready_in. Request-log entry shape unchanged.

## Testing

- Expanded specialized name patterns.
- Classifier: Gemini-style 404 → cool; DeepSeek `reasoning_content` 400 → do not cool.
- Backoff timing and routing skip while cool.

## Docs

- Configuration: filter note for large discovery limits; unsuitable cooldown brief.
- Routing: failover note that unsuitable models cool with exponential backoff.
