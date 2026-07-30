# Request Log: Separate Failed vs Skipped Cascades

**Date:** 2026-07-30  
**Status:** Approved  
**Approach:** Structured cascade trail on each request-log entry; dashboard shows both counts with one clickable full-path detail

## Problem

The Live Request Log shows a single **Cascades** count (`attempts - 1`). That count only reflects failed `forward()` calls before the winner. Pre-contact skips (token cap, no tools/vision, circuit open, access scope, rate headroom exhausted, keys cooling) appear only in server logs and are invisible in the dashboard. Operators cannot tell *which* models were abandoned or *why*.

## Goals

1. Split cascade observability into **failed** (upstream contacted, did not succeed) and **skipped** (never contacted).
2. Show **both numbers** in the request-log table.
3. Make the cell **one clickable control** that opens the **full cascade path**, including the winning model when the request succeeded.
4. Persist a structured trail on each ring-buffer log entry so reasons survive for the life of the buffer.

## Non-goals

- Changing candidate ranking, soft headroom preference, or when the router walks candidates.
- Dropping zero-headroom models from the candidate list (dashboard/log only).
- VS Code extension UI (no request-log table there).
- Prometheus metrics for failed vs skipped.
- Storing raw upstream response bodies in the trail (size + sensitivity).

## Background: why low-headroom models appear

Ranking already soft-prefers rate headroom (`_rate_score` in `_get_smart_ordered`), but candidates stay in the list. At attempt time, thin headroom still tries (debit may fit); only when `check_and_consume` fails with a long wait does the router skip without calling upstream. Sticky can also pin a model ahead of rank. That is intentional routing behavior; this design only makes those skips visible as `skipped` / `rate_headroom`.

## Classification

| Outcome | Meaning | Examples |
|---|---|---|
| `skipped` | Never called upstream for this `(provider, model)` | token cap, no tools, no vision, circuit open, access scope, rate headroom exhausted, keys cooling, tool-deferred first pass |
| `failed` | `forward()` ran and did not become the winner | network/timeout, HTTP 429 / 4xx / 5xx that caused continue or break |
| `success` | Winner that served the request | final step when routing succeeded |

**Rate headroom exhausted** (no `forward()`) → `skipped`, not `failed`.

## Data model

Each request-log entry gains:

```json
{
  "failed": 1,
  "skipped": 2,
  "cascades": 3,
  "cascade": [
    {"provider": "groq", "model": "llama-3.1-8b", "outcome": "skipped", "reason": "rate_headroom"},
    {"provider": "cerebras", "model": "llama3.1-8b", "outcome": "failed", "reason": "http_429"},
    {"provider": "openrouter", "model": "…", "outcome": "success", "reason": null}
  ]
}
```

- `failed` / `skipped` — counts of those outcomes in `cascade` (the `success` step is not counted in either).
- `cascades` — `failed + skipped`. Field name kept for consumers that still read a single number; **semantics widen** vs today (`attempts - 1`, failed forwards only) because skips are now included. Prefer `failed` / `skipped` for new code.
- `cascade` — ordered trail of steps for this request. Exhausted/error requests with no winner omit `success`. First-try wins still append a single `success` step.
- `reason` — short stable code (not free text from upstream bodies). Codes cover every existing skip/fail branch that abandons a candidate; known set includes `rate_headroom`, `token_cap`, `no_tools`, `no_vision`, `circuit_open`, `access_scope`, `keys_cooling`, `http_429`, `http_401`, `http_403`, `http_400`, `http_404`, `http_413`, `http_5xx`, `network`. UI maps codes to human-readable labels; unknown codes display as the raw code.

**Per-model, not per-key:** multiple keys on the same model produce **one** trail step for that model’s outcome, preferring the most informative reason (e.g. `http_429` over `keys_cooling` if both occurred). Avoid per-key noise in the trail. A model deferred on the tools first pass and later tried in last-resort may appear twice (first `skipped`/`no_tools`, then later `failed` or `success`) — that is intentional.

**Cache hits / early exits with no candidate walk:** `cascade: []`, `failed: 0`, `skipped: 0`, `cascades: 0`.

## Recording

- Append steps during the `_route_completion` walk (and the embeddings path where the same skip/fail pattern exists) onto `_req_ctx` (e.g. `_req_ctx.cascade = []`).
- `_log_completion` copies the list into the log entry and derives counts.
- On success, append a final `success` step for the winning `(provider, model)`.
- Recording is **fail-soft**: a bad append must never break routing; `_log_completion` already fails soft — same rule for the new fields.
- **No routing behavior changes** — observability only.

## Dashboard UI

**Table**

- Replace the Cascades column header with **Fail / Skip**.
- Cell shows both numbers, e.g. `1 / 2`; muted `0 / 0` when empty.
- When `cascade` is a non-empty array (including a lone `success` step), the **whole cell** is one clickable control — not separate targets for fail vs skip.

**Detail modal**

- Reuse the existing rate-limit modal pattern (overlay, Escape to close, click-outside to close).
- Title: request time + endpoint + final status.
- Body: ordered list/table — provider, model, outcome pill (`skipped` / `failed` / `success`), human-readable reason from the code.
- Empty trail: cell not clickable (or no-op).

**Legacy entries** (mid-deploy buffer entries missing the new fields): fall back to legacy `cascades` as a single non-clickable number (or `N / —`) so the table does not break.

## API

`GET /v1/logs` continues to return ring-buffer entries as today; new fields appear on each entry. No separate cascade detail endpoint.

## Testing

- Unit: derived counts from a synthetic trail; reason-code → label mapping.
- Routing/log integration (existing test patterns): skip path → `skipped` + expected reason; failed `forward` → `failed` + status reason; success → final `success` step.
- Dashboard fallback: legacy-shaped entries still render without throwing.

## Docs

Update `documentation/monitoring.md` and its website mirror to describe Fail/Skip and the cascade detail view instead of a single cascade count.

## Out of scope (recap)

Routing/ranking changes, VS Code extension, Prometheus split metrics, raw response bodies in the trail, separate per-request detail API.
