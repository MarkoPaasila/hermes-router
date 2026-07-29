# Adaptive Token Bucket Rate Limiter — Design Spec

**Date:** 2026-07-29  
**Project:** hermes-router  
**Status:** Approved, awaiting implementation plan

---

## 1. Goal

Replace hermes-router's reactive 429-then-cooldown model with a proactive, self-adjusting
multi-window token bucket filter. The system learns each upstream provider's real rate limits
from response headers and 429 signals, persists that knowledge across restarts, and uses it
for both request gating (pacing) and headroom-aware provider selection (routing).

---

## 2. Scope

Two complementary behaviours:

1. **Proactive pacing** — hold or briefly delay a request when a learned cap is nearly
   exhausted; fail over to the next candidate when the wait would be too long.
2. **Headroom-aware routing** — score candidates by remaining capacity across all active
   windows before selecting the best (key, model) to send to.

**TBF is the sole upstream rate-limit mechanism.** CredentialPool `mark_rate_limited`
cooldowns are not used. A 429 (or a 2xx body that is clearly a quota/rate-limit error)
triggers the adaptive learning loop and, when present, a `Retry-After` hold on the
bucket group (`blocked_until`) so refill math cannot re-admit that scope early.
Network/5xx health failures still use CredentialPool `mark_key_down`.

---

## 3. Bucket Granularity

Buckets are scoped at two levels for each upstream API key:

| Group | Scope |
|---|---|
| Provider-wide | (provider_key) |
| Model-specific | (provider_key, model) |

Each group can hold active buckets for up to 10 logical limits:

- **Request windows:** RPM, RPH, RPD, RPW, RPMo (minute/hour/day/week/month)
- **Token windows:** TPM, TPH, TPD, TPW, TPMo

Window durations in seconds: 60 / 3600 / 86400 / 604800 / 2592000.

---

## 4. TokenBucket Mechanics

Each `TokenBucket` instance tracks one window × one dimension (requests or tokens).

**State fields:**
- `cap` — learned rate limit for this window. Starts from the defaults table (§6).
- `tokens` — current available capacity; float, refills continuously.
- `window_seconds` — duration of this window.
- `last_refill` — Unix timestamp of last refill computation.
- `active` — bool; inactive buckets are excluded from all math.

**Refill rule:** on every access, compute elapsed seconds since `last_refill`, add
`elapsed × (cap / window_seconds)` to `tokens`, clamp to `cap`, update `last_refill`.

**Consume rule:** debit `tokens` by the request cost (1 for R-buckets, estimated token
count for T-buckets). Consumption is **optimistic** (before response); reconciled after
with actual usage from the response body.

**Pre-request estimate for token buckets:** use the existing token-size estimation path
(same logic as `SKIP_TOKENS_OVER` checks). After response, debit any surplus or restore
any unused estimate.

---

## 5. Learning Loop

### 5.1 Header-driven (takes precedence)

When a response includes standard rate-limit headers, overwrite bucket state precisely:

| Header | Action |
|---|---|
| `x-ratelimit-limit-requests` / `x-ratelimit-limit-tokens` | Set `cap` |
| `x-ratelimit-remaining-requests` / `x-ratelimit-remaining-tokens` | Set `tokens` |
| `x-ratelimit-reset-requests` / `x-ratelimit-reset-tokens` | Adjust `last_refill` |

Anthropic, OpenAI, and compatible providers send these. For minute-window headers the
relevant bucket is RPM/TPM; for day-window headers RPD/TPD, etc. Inferred values always
yield to hard header data.

### 5.2 Signal-driven (no headers)

- **429 received:** for each active bucket, cut `cap` to `max(1, observed_rate × 0.8)`
  and set `tokens` to 0. `observed_rate` is computed per-bucket: the total requests (or
  tokens) consumed from that bucket since its last full refill period. For buckets with no
  meaningful history (fewer than 3 requests recorded), the cut instead halves the current
  `cap` rather than using a potentially noisy rate estimate. When a `Retry-After` header
  is present, also set the group's `blocked_until` so `check_and_consume` rejects until
  that time even if refill would otherwise restore tokens.
- **20 consecutive successes without hitting the cap:** nudge `cap` up by 5%, clamped
  to a configurable ceiling (`RATE_LEARN_MAX_MULTIPLIER`, default 10× the initial default).

### 5.3 Inactive bucket heuristic

After each full window period, if a bucket:
- has never been the binding constraint (never reached 0 tokens), **and**
- processed fewer than `max(10, cap × 0.1)` requests in that period

→ mark it `active = False`. It stays dormant until a 429 or explicit header re-activates it.
This prevents long-window buckets (week/month) from accumulating noise during low-traffic periods.

---

## 6. Default Caps Table

Built-in starting caps by provider name (free-tier conservative estimates):

| Provider | RPM | TPM | RPD |
|---|---|---|---|
| groq | 30 | 6 000 | 14 400 |
| gemini | 15 | 32 000 | 1 500 |
| openrouter | 20 | 20 000 | — |
| mistral | 5 | 16 000 | — |
| cohere | 20 | 10 000 | — |
| nvidia | 40 | 40 000 | — |
| _default | 10 | 10 000 | — |

Hour/day/week/month windows without an explicit entry start **uncapped** (infinite bucket,
not enforced) until a 429 or header teaches a real limit.

**Override precedence (highest first):**
1. `x-ratelimit-*` response headers
2. `rate_defaults` section in `auth.json`
3. `RATE_DEFAULT_<PROVIDER>_<WINDOW>` env vars (e.g. `RATE_DEFAULT_GROQ_RPM=60`)
4. Built-in table above

---

## 7. Routing Integration

### 7.1 Headroom score

Before the existing provider-ordering pass, compute a headroom score for each (key, model)
candidate:

```
headroom = min(b.tokens / b.cap for b in active_buckets)   # 0.0 – 1.0
```

The score is the fraction of the tightest active window remaining. It acts as a multiplier
on the existing provider rating, so candidates with more headroom rank higher within an
equal-rating tier.

### 7.2 Send-time gate

After a candidate is selected:

1. Check all active buckets for both the provider-wide group and the model-specific group.
2. If all show `tokens ≥ cap × RATE_HEADROOM_THRESHOLD` (default 0.05, i.e. 5% of cap):
   consume optimistically and send.
3. If any bucket is below threshold but predicted refill delay ≤ `RATE_SHORT_WAIT_MS`
   (default 500 ms): sleep once, re-check, then send.
4. Otherwise: skip this (key, model) — falls through to the existing failover path.

### 7.3 Reconciliation

After receiving the response (including streamed usage chunks):
- Debit any token surplus vs. the pre-request estimate from T-buckets.
- If the actual count was lower, restore the unused portion to T-buckets (clamped to `cap`).

---

## 8. Persistence

**File:** `rate_limits_state.json` (same directory as `router_state.json`).

### 8.1 Schema

```json
{
  "version": 1,
  "groups": {
    "provider:groq|key:abc123": {
      "RPM": {"cap": 30, "tokens": 18.4, "last_refill": 1753779600.0},
      "TPM": {"cap": 6000, "tokens": 3200.0, "last_refill": 1753779600.0}
    },
    "provider:groq|key:abc123|model:llama-3.3-70b": {
      "RPM": {"cap": 20, "tokens": 12.1, "last_refill": 1753779600.0}
    }
  }
}
```

Inactive buckets are not written. Key suffix in the group key is the last 8 characters
of the API key (same convention as existing log redaction).

### 8.2 Flush strategy

- Periodic background flush every 60 seconds.
- Flush on clean shutdown (SIGTERM/SIGINT handler, extending existing state-save logic).
- Not flushed on crash — buckets restore from defaults and refill naturally.

### 8.3 Load on boot

1. Read `rate_limits_state.json` if present; skip silently if missing or corrupt.
2. Restore `cap` values immediately.
3. Restore `tokens` conservatively: `min(persisted_tokens, cap × 0.5)` — prevents a
   post-restart burst from a stale full bucket.
4. If `last_refill` is within one window period of now: compute elapsed refill and add it.
   Otherwise treat bucket as full (enough time has passed that the provider window reset).

---

## 9. Observability

### 9.1 Dashboard

Extend the existing per-key/model rows:
- Add a **"Rate headroom"** column showing the tightest active window as a coloured
  progress bar (green ≥ 50%, yellow 20–50%, red < 20%), matching the existing RPM bar style.
- Tooltip on hover: each active window's `used/cap` for both R and T dimensions.

### 9.2 `/v1/status` JSON

Extend per-provider stats with a `rate_limits` object:

```json
"rate_limits": {
  "provider_wide": {
    "RPM": {"cap": 30, "used": 18, "headroom": 0.40}
  },
  "models": {
    "llama-3.3-70b": {
      "RPM": {"cap": 20, "used": 12, "headroom": 0.40},
      "TPM": {"cap": 6000, "used": 3200, "headroom": 0.47}
    }
  }
}
```

### 9.3 Logging levels

| Event | Level |
|---|---|
| Bucket consume / refill | DEBUG |
| Cap updated (429 cut or success nudge) | INFO |
| Bucket marked inactive (heuristic drop) | INFO |
| Bucket re-activated (429 or header) | INFO |
| Retry-After hold applied | INFO |
| Health-key cooldown (`mark_key_down`) | WARNING |

---

## 10. Configuration Reference

| Env var | Default | Description |
|---|---|---|
| `RATE_SHORT_WAIT_MS` | 500 | Max ms to sleep waiting for a thin bucket to refill |
| `RATE_HEADROOM_THRESHOLD` | 0.05 | Fraction of cap below which a bucket is considered thin |
| `RATE_LEARN_SUCCESS_STREAK` | 20 | Consecutive successes before nudging cap up |
| `RATE_LEARN_NUDGE_PCT` | 5 | Percent to increase cap on a success nudge |
| `RATE_LEARN_CUT_FACTOR` | 0.8 | Multiplier applied to observed rate on 429 |
| `RATE_LEARN_MAX_MULTIPLIER` | 10 | Cap ceiling as multiple of initial default |
| `RATE_DEFAULT_<PROVIDER>_<WINDOW>` | — | Per-provider cap override (e.g. `RATE_DEFAULT_GROQ_RPM=60`) |
| `RATE_STATE_FLUSH_INTERVAL` | 60 | Seconds between background persistence flushes |

---

## 11. What Is Not In Scope

- Key-pooling aggregates across keys for the same provider (deferred to a future spec).
- UI to manually override a learned cap from the dashboard (env var override is sufficient for now).
- Metrics export of per-bucket state to Prometheus (can be added incrementally).
