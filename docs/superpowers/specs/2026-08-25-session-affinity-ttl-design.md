# Session affinity idle TTL (300s default)

**Date:** 2026-08-25  
**Status:** approved

## Goal

Align session-affinity idle expiry with typical upstream **prompt-cache**
windows (~5 minutes). Affinity’s main value is cache hits on the same
`(provider, model, key)`; pinning longer than that window rarely helps and
delays returning to full-catalog selection.

## Terminology

Prefer **session affinity** over “stickiness” (`CONTEXT.md`).

| Term | Meaning |
|------|---------|
| **Idle TTL** | Sliding timeout from last successful affinity `set` (`updated_at`). |
| **SESSION_AFFINITY_TTL_SECONDS** | Env knob for that idle TTL. |

## Context (current behavior)

- `SessionStickyStore` remembers `(provider, model, key)` per client session id.
- Default idle TTL was **3600s**; refreshed on each successful `set`.
- Affinity also clears on cascade leave / explicit `clear` (unchanged).
- Response cache uses separate `CACHE_TTL_SECONDS` (default 300); unrelated.

## Decision summary

1. Default idle TTL: **300 seconds**.
2. Env: **`SESSION_AFFINITY_TTL_SECONDS`** (default `300`).
3. **`0` = no idle expiry**; cascade/`clear` still drop affinity.
4. Invalid env → `300`; negative → `0` (`max(0, …)`).
5. Store: skip idle expiry when `ttl_s <= 0` (otherwise `age > 0` expires immediately).
6. Independent of `CACHE_TTL_SECONDS`.

## Architecture

```
request with session id
  → sticky_store.get(sid)
       age > ttl_s (and ttl_s > 0)? → miss (full catalog)
       else → promote affine candidate
  → success → sticky_store.set(...)  # refreshes updated_at
  → cascade leave affine → clear
```

No change to session-id resolution, key affinity, or cascade rules.

## Components

### `SessionStickyStore` (`session_sticky.py`)

- Default `ttl_s=300.0`.
- `get` / `_evict_unlocked`: if `ttl_s <= 0`, never idle-expire.

### Router wiring (`router.py`)

- `SESSION_AFFINITY_TTL = max(0, parsed SESSION_AFFINITY_TTL_SECONDS)` default 300.
- `sticky_store = SessionStickyStore(ttl_s=SESSION_AFFINITY_TTL)`.

## Error handling

- Bad env strings fall back to 300 (same pattern as other int env knobs).
- Expired affinity is equivalent to “no pin” — not an error.

## Testing

- Idle expiry after TTL (existing).
- `ttl_s=0` survives long idle; `clear` still removes.
- Docs/config list the new env var.

## Out of scope

Response cache, capability sticky-merge, key-affinity-until-fail, absolute
(hard) affinity lifetime.
