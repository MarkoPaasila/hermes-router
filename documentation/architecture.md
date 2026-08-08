# Architecture — How it works

hermes-router is a single Python file (`router.py`) running a small Flask/Waitress server. It
accepts OpenAI- or Anthropic-format requests and forwards each one to the best available
provider in a pool, handling key affinity, fallback, and format translation transparently.

## The request pipeline

Every request flows through the same pipeline:

```
  ┌──────────┐   OpenAI- or Anthropic-format    ┌─────────────────────────────────────┐
  │ Your app │ ───────────────────────────────► │            hermes-router            │
  └──────────┘   Bearer / x-api-key (access key)  │                                     │
       ▲                                          │  1. Auth check (constant-time)      │
       │                                          │  2. Cache lookup (per-caller)       │
       │            OpenAI/Anthropic response     │  3. Rate the request 1–5            │
       └────────────────────────────────────────►│  4. Order full catalog by fit       │
                                                  │  5. Try candidates, key affinity     │
                                                  └───────────────┬─────────────────────┘
                                                                  │ first one that succeeds
                                                  ┌───────────────▼─────────────────────┐
                                                  │ Gemini · OpenRouter · Groq · Mistral │
                                                  │ Cohere · NVIDIA · Codex · Kimi (16)  │
                                                  └──────────────────────────────────────┘
```

1. **Authenticate** — the caller's key is compared against `PROXY_API_KEYS` in constant time
   (`hmac.compare_digest`). Both `Authorization: Bearer` and Anthropic's `x-api-key` are accepted.
2. **Cache lookup** — identical requests can be served from an in-memory cache, namespaced by
   the calling key (see [Response cache](#response-cache)).
3. **Rate the request** — a 1–5 complexity score is computed from length and content, with no
   extra API call.
4. **Order the catalog** — every configured `(provider, model)` has a GI score (0–100);
   the proxy prefers the *cheapest* candidate that meets the complexity→min-GI threshold, skips
   unhealthy ones, and promotes a session-affinity match to the front when present.
5. **Try and fall back** — it sends to the first candidate, preferring the affine key when
   set; on rate-limit or error it cascades to the next catalog entry, clearing stickiness when
   leaving the affine `(provider, model)`.

## The moving parts

### Credential pool

Every provider can hold many keys (from `auth.json` first, then `.env`). Keys are tracked in a
thread-safe pool with **key affinity** selection: the proxy keeps using the same key for a
`(provider, model)` until that key errors or is rate-limited, then tries the next ready key in
stable deque order. **Upstream rate limits** are tracked by the adaptive token-bucket filter
(adaptive rate limiter): it learns caps from response headers and 429s, **force-admits** when estimates look
empty (explore-into-limit), ranks by headroom for new sessions / after bumps, and cascades on
real `Retry-After` holds or upstream 429s. Header snaps are authoritative for that model bucket (success
AIMD nudges are skipped while pinned). New buckets start at half fill; failed upstream attempts
after admit release the reservation. Header updates carry request-start time so older in-flight
responses cannot overwrite newer state. Each rate-limit group keeps a **full-grid ledger** — all ten
`[R,T]×[M,H,D,W,Mo]` buckets are always on (never auto-deactivated). Hard 429s cut a **single**
ladder-attributed bucket then reclamp the chain; minute windows are the fast learner, longer
windows grow slowly on success and drop on attributed 429s. The credential pool's
per-key cool-downs are only for **health** failures (network errors / 5xx via `mark_key_down`),
not for rate limits.

Each key's usage count is tracked and exposed per provider in `/v1/status` (`keys[].requests`)
and as a tooltip on the web dashboard's key dots.

**Multiple models per provider.** A provider's `<PROVIDER>_MODEL` can be a comma-separated
list. Because free-tier rate limits are per-**model**, Rate-limit buckets are tracked per **(key,
model)** (plus a provider-wide group): when one model is rate-limited, the proxy falls back to
the next model on the same key before cascading to the next provider.

> Provider-wide buckets are a shared-ceiling **estimate** (no header sync; softer cuts; faster recovery), initialized at **×10** the model/base default caps so shared-ceiling headroom % does not mirror the model bar. Model buckets remain authoritative for that model's upstream limits.

This multiplies free
capacity along a third axis — **keys × models × providers** — with no extra signups. Each listed
model is also a first-class routing candidate (see [Catalog selection](#smart-routing) below), not
just fallback. See [Configuration](configuration.md#multiple-models-per-provider).

### Catalog selection

Requests are scored for complexity (1–5) and models for **general intelligence ranking**
(GI, 0–100). Each complexity level maps to a minimum GI; the proxy builds a **full catalog** of
every configured `(provider, model)` pair and picks the cheapest candidate that clears the bar.
Tool requests are only sent to models that
support tool calling (detected at startup). Optional **fast routing**
(`FAST_ROUTE_THRESHOLD`) sends short requests to low-latency providers first.

**Session affinity.** When the client sends a session id (header `X-Hermes-Session-Id` or
`X-Chat-ID`, body `user`, or `metadata.session_id` / `sessionId` — first non-empty wins; the
router never invents one), the proxy remembers the winning `(provider, model, key)` and reuses
it on later chat turns until it cascades away from that model. With no session id, every request
gets a fresh catalog pick. The Hermes Agent extension does not yet forward session ids to custom
endpoints.

**Per-model scoring.** When a provider lists several models, each **(provider, model)** pair is its
own catalog candidate, scored on *its own* GI and tool/reasoning support — not the primary's.
So with `GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-pro`, an easy turn goes to `flash-lite` while
a hard or tool-using turn can pick `gemini-2.5-pro`, instead of the extra models only being used for
rate-limit fallback. Within equal price, lower GI overshoot wins; a provider's models keep their
**listed order** as a final tie-break. Tool/reasoning support is detected per model, so models of
different classes can safely share one list. Each model's GI shows in `/v1/status` under
`model_caps` (`gi`, `gi_source`).

**Local models & fast preference.** A model running on your own machine (Ollama / LM Studio /
llama.cpp) can join the pool as the `local` provider — free, private, fast (see
[Providers](providers.md#local-models-ollama--lm-studio--llamacpp)). Sending the model id
`hermes-router:fast` (or header `X-Hermes-Profile: fast`) makes the proxy prefer that local
model for short/casual turns, with the cloud providers as automatic fallback for heavier
requests.

### Fallback & circuit breaker

If a provider errors or times out, the proxy cascades to the next automatically. Each attempt
also has a **TTFT deadline** from that candidate’s EWMA baseline (or a cold absolute): if
headers take too long, the attempt aborts, session affinity clears when that candidate was
affine, and the cascade continues — without tripping the breaker. A provider that keeps failing
health checks (network errors or 5xx — not rate-limits, bad requests, or TTFT aborts) has its
**circuit breaker** tripped: it's pulled out of rotation for a cooldown, then re-probed
(half-open). Healthy providers are always preferred. Tunable via the `BREAKER_*` and `TTFT_*`
settings.

### Response cache

Identical requests can be served from an in-memory TTL+LRU cache, saving free-tier quota. Cache
entries are **namespaced by the caller's API key**, so two different `PROXY_API_KEYS` never share
a cached answer for the same prompt — safe to expose to multiple users. Disable with
`CACHE_TTL_SECONDS=0`.

**Semantic cache** (opt-in, `SEMANTIC_CACHE=1`) goes a step further: on an exact-match miss it
embeds the prompt (reusing the router's own embeddings pipeline) and returns a cached answer
whose stored prompt is *similar* above `SEMANTIC_CACHE_THRESHOLD` (cosine). It's a bounded linear
scan over the LRU within the caller's namespace, and degrades gracefully to exact-match when no
embedding provider is available — so it adds savings without changing behavior when off.

### Per-key budgets & rate limits

Each `PROXY_API_KEYS` entry can carry a requests-per-minute ceiling and per-UTC-day request,
token, **and estimated-cost** budgets (set globally via `PROXY_LIMIT_*` or per key in `auth.json`
with `hr limit`). A caller over its limit gets a `429` with `Retry-After` *before* any provider is
contacted; live counters appear in `/v1/status`. Unset = unlimited, so single-user setups are
unaffected. This makes the proxy safe to share with a team. See
[Configuration](configuration.md#per-key-budgets--rate-limits).

**Cost awareness.** Spend is estimated from a built-in per-model price table (free providers and
subscription plans are `$0`) and surfaced per provider and per key in `/v1/usage`, `/v1/status`,
and the dashboard (USD). See
[Configuration](configuration.md#cost--spend-awareness).

### Accurate token counting

Request size is measured with `tiktoken` (the `o200k_base` encoder, loaded lazily) for accurate
routing and large-payload skipping, with a `characters ÷ 4` fallback when tiktoken is unavailable.

### Feature probing

At startup the proxy probes each provider once to learn its real model, whether it supports
**tool calling**, and whether it's a **reasoning model**. Results are cached to
`router_state.json` for `ROUTER_STATE_TTL_HOURS` (default 24h) so restarts don't re-probe. You
can override any result with `<PROVIDER>_SUPPORTS_TOOLS` / `<PROVIDER>_REASONING`.

Reasoning models spend output tokens on hidden chain-of-thought, so the proxy reserves extra
output budget (`REASONING_TOKEN_RESERVE`) to stop a small `max_tokens` from yielding an empty reply.

### Request guardrails

The proxy defends itself and avoids wasted upstream calls:

- **Body-size limit** — requests larger than `MAX_REQUEST_BYTES` (default 10 MB) are rejected
  with `413` before any provider is contacted, so a buggy client can't exhaust memory.
- **Large-payload skip** — some free tiers reject big requests outright (e.g. Groq ~6K
  tokens/min → `413`). When a request is estimated to exceed a provider's ceiling
  (`<PROVIDER>_SKIP_TOKENS_OVER`), that provider is skipped and the proxy cascades on instead of
  burning a guaranteed-failed attempt.
- **Output clamp** — providers that `400` when `max_tokens` exceeds their output cap have the
  requested output transparently clamped down to their ceiling (`<PROVIDER>_MAX_OUTPUT_TOKENS`),
  so the call still succeeds.

### Concurrency

The server runs on Waitress with a configurable thread pool (`WORKER_THREADS`, default 16). The
upstream HTTP connection pool scales with that automatically, and streaming responses close their
upstream connection cleanly when the stream ends or the client disconnects.

## Protocol translation

Clients speak OpenAI Chat Completions; the proxy adapts to whatever the chosen provider needs.

| Provider type | Wire format | How the proxy handles it |
|---|---|---|
| Most providers | OpenAI Chat Completions | Pass-through (the router's native format) |
| Anthropic (provider) | Messages API outbound | Request/response translation incl. tools & streaming |
| Codex (ChatGPT) | **Responses API** over OAuth | Two-way translation + OAuth token lifecycle |

- **Anthropic as a provider** — when the catalog picks Anthropic, the proxy translates the
  OpenAI-format request to Anthropic Messages and translates the reply back.
- **Codex (ChatGPT subscription)** — authenticates with OAuth, not an API key. Accounts are
  imported with `hr auth import-codex`; the proxy mints fresh access tokens from the refresh
  token, sends requests to the ChatGPT backend in Responses-API format, and translates the SSE
  stream back to OpenAI chunks. Multiple accounts pool naturally with key affinity key
  selection. See [Providers](providers.md#codex-chatgpt-subscription).

## Endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | access key | OpenAI chat completions (streaming + tools) |
| `POST /v1/embeddings` | access key | OpenAI embeddings (stable provider order) |
| `GET /v1/models` | access key | Advertises `hermes-router` (and `:fast` when local is configured) plus every live chat catalog model id |
| `GET /v1/status` | access key | Per-provider health, latency, keys, key-affinity mode, cache |
| `GET /health` | none | Liveness check for uptime monitors |

## Observability

`hr status` renders a live terminal summary (provider health, latency, key cooldowns, cache) from
`/v1/status`. The web dashboard at `/dashboard` shows the same picture interactively. Rate
headroom appears on the dashboard **Providers** (provider-wide buckets) and **Models**
(combined capabilities + per-model headroom) pages. See [Monitoring](monitoring.md).

## Ways to run and connect

The same `router.py` engine runs everywhere; you choose how to launch it.

**Run it:**

- **`hr` CLI** *(Linux/macOS/WSL)* — `hr setup`, `hr auth add`, `hr status`, `hr restart`. The
  friendly day-to-day way to manage a local router. See [Deployment](deployment.md).
- **Native Python** — `python router.py` (or `hr start`) on any OS with Python 3.10+.

**Connect to it:**

- **Any OpenAI SDK** — point `base_url` at the proxy and you're done. See [Usage](usage.md).

## Design principles

- **Self-contained** — one Python file; keys live in your own `auth.json` (git-ignored, `0600`).
  Nothing is installed system-wide beyond the `hr` symlink.
- **Configured by environment** — every behavior is an env var with a sensible default; see
  [Configuration](configuration.md).
- **Core vs. add-ons** — a small set of **core** features is always on (auth, fallback, smart
  routing, the circuit breaker…); everything optional is an **add-on** you toggle with
  [`hr features`](configuration.md#hr-features--see-and-toggle-add-ons). Add-ons default to off
  (so a fresh install is minimal) and never change core behavior.
- **Fail soft** — when in doubt the proxy makes forward progress (e.g. if every provider's
  breaker is open it probes them all) rather than hard-failing while options remain.

---

**Next:** [Monitoring](monitoring.md) — web dashboard, `hr status`, and usage endpoints.
