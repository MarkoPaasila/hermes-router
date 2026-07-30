---
title: "Configuration"
description: "auth.json, every .env setting, model overrides, sticky key selection and advanced knobs."
---

All configuration is via environment variables (in `.env`) and the `auth.json` credential
store. Everything is optional with sensible defaults — the router runs out of the box once
it has at least one key.

## Core features vs. add-ons

hermes-router splits its behavior into two groups:

- **Core features** — always on; they *are* the router. Auth, the credential pool + sticky key
  selection, failover, the circuit breaker, smart routing, protocol translation
  (OpenAI/Anthropic/Codex), capability probing, token counting, request guardrails, and
  usage/cost tracking. You don't configure these on/off.
- **Add-ons** — optional behaviors you turn on when you want them. Each is backed by an
  environment variable (or some `auth.json` config), and unset = off (except the response
  cache, on by default).

### `hr features` — see and toggle add-ons

```bash
hr features list                      # core features + every add-on, with on/off
hr features enable persistent_cache   # writes the backing var to .env
hr features disable semantic_cache
hr restart                            # apply
```

| Add-on | Backing setting | Default | What it does |
|---|---|---|---|
| `response_cache` | `CACHE_TTL_SECONDS` | **on** | Serve identical requests from an in-memory TTL+LRU cache |
| `semantic_cache` | `SEMANTIC_CACHE` | off | Also serve cached answers for *similar* prompts |
| `persistent_cache` | `CACHE_PERSIST` | off | Mirror the cache to SQLite so it survives restarts |
| `fast_routing` | `FAST_ROUTE_THRESHOLD` | off | Short requests prefer low-latency providers on ties |
| `model_discovery` | `AUTO_DISCOVER_MODELS` | off | Refresh provider model lists from `/models` at startup |
| `filter_specialized_models` | `FILTER_SPECIALIZED_MODELS` | off | Drop TTS/STT/image-gen/OCR/video/embedding/moderation/rerank IDs from discovery |
| `metrics_auth` | `METRICS_REQUIRE_AUTH` | off | Require the proxy key on `/metrics` |
| `cost_currency` | `COST_FX_RATE` | off | Show a second currency (e.g. ₹) alongside USD spend |
| `key_budgets` | `auth.json` / `PROXY_LIMIT_*` | off | Per-key RPM / daily request / token / cost ceilings — manage with `hr limit` |
| `local_model` | `LOCAL_BASE_URL` / `LOCAL_MODEL` | off | Route to a model on your own machine — manage with `hr model set local` |

`hr features enable/disable` toggles the simple **flag** add-ons by writing their variable to
`.env`. The last two are richer config, so `hr features` shows their status and points you to
the command that manages them (`hr limit`, `hr model`). The live state is also in `/v1/status`
under `features` and in the VS Code dashboard.

## Where your keys live

`hr auth add` writes to **`auth.json`** — the router's own credential store, kept next to
the router. It's git-ignored, so real keys are never committed. Codex (ChatGPT
subscription) logins are stored separately under `codex_accounts` (via
`hr auth import-codex`); the router refreshes their OAuth access tokens automatically.

```json
{
  "providers": {
    "openrouter": ["sk-or-key1", "sk-or-key2"],
    "gemini": ["AIzaSy-key"]
  }
}
```

> Keys in `.env` (e.g. `OPENROUTER_API_KEYS=k1,k2`) still work too — the router reads
> `auth.json` first, then falls back to `.env`. Point at a different file with
> `ROUTER_AUTH_FILE=/path/to/auth.json`.

## Settings (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8319` | Port to listen on |
| `HOST` | `0.0.0.0` | Bind address. Set `127.0.0.1` to listen on localhost only (recommended on a shared/VPS host — reach it via localhost or an SSH tunnel). Keep `0.0.0.0` for Docker. |
| `PROXY_API_KEYS` | *(auto-generated)* | Comma-separated keys your app uses to authenticate — and the key needed to open the web dashboard. If left unset (or on the `.env.example` placeholder), the router generates a real random key on first boot and saves it back to `.env`, logging it once. Add more from the dashboard's **Access Keys** page, or set your own here. |
| `ROUTER_AUTH_FILE` | `./auth.json` | Where keys are stored |
| `CACHE_TTL_SECONDS` | `300` | Response cache lifetime (`0` disables). Entries are namespaced per API key, so different `PROXY_API_KEYS` never share a cached answer — safe for multi-tenant use |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `METRICS_REQUIRE_AUTH` | `0` | Require the proxy key on `/metrics` (`1` to enable) |
| `REASONING_TOKEN_RESERVE` | `4096` | Extra output budget added for reasoning models so hidden chain-of-thought doesn't eat the answer (`0` disables) |

### Advanced settings

Sensible defaults — most users never touch these.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_REQUEST_BYTES` | `10485760` (10 MB) | Max request body size; larger requests get `413` (guards against memory exhaustion) |
| `WORKER_THREADS` | `16` | Waitress worker threads (concurrency). The HTTP connection pool scales with this |
| `CACHE_MAX_SIZE` | `100` | Max entries in the response cache (LRU eviction) |
| `CACHE_PERSIST` | `0` | If `1`, mirror the cache to a SQLite file so it survives restarts (opt-in). The DB mirrors the in-memory LRU, so it stays bounded by `CACHE_MAX_SIZE` — raise that to persist more |
| `CACHE_DB_PATH` | `./cache.db` | SQLite file for the persistent cache. On read-only hosts (e.g. HF Spaces) point it at `/tmp/cache.db` |
| `SEMANTIC_CACHE` | `0` | If `1`, also serve cached answers for *similar* prompts (needs an embedding provider; falls back to exact match otherwise) |
| `SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine-similarity cutoff for a semantic hit (`1.0` = identical; lower = looser matching) |
| `FAST_ROUTE_THRESHOLD` | `0` | If >0, requests under this many tokens prefer low-latency providers first (`0` disables) |
| `AUTO_DISCOVER_MODELS` | `0` | If `1`, fetch configured providers' `/models` lists at startup, prune listed models that disappeared, and append the best discovered models |
| `AUTO_DISCOVER_MODEL_LIMIT` | `8` | Max models kept per provider when `AUTO_DISCOVER_MODELS=1` |
| `FILTER_SPECIALIZED_MODELS` | `0` | If `1`, drop purpose-specific models from auto-discovery catalogs (configured `{PROVIDER}_MODEL` lists are never filtered) |
| `{PROVIDER}_EXCLUDE_MODELS` | — | Comma-separated model IDs to block for a provider (case-insensitive). Excluded models are stripped from config and discovery, e.g. `OPENROUTER_EXCLUDE_MODELS=some/model:free` |
| `ROUTER_MODEL_ID` | `hermes-router` | The model name clients send (the router maps it to each provider's real model) |
| `ROUTER_STATE_FILE` | `./router_state.json` | Where provider ratings/capabilities are cached between restarts (use `/tmp/...` on read-only hosts like HF Spaces) |
| `ROUTER_STATE_TTL_HOURS` | `24` | How long the cached probe state is trusted before re-probing (`0` = re-probe every start) |
| `BREAKER_WINDOW` | `8` | Recent outcomes the circuit breaker weighs per provider |
| `BREAKER_MIN_SAMPLES` | `4` | Minimum samples before the breaker can trip |
| `BREAKER_ERROR_RATE` | `0.5` | Health-failure fraction that trips the breaker |
| `BREAKER_COOLDOWN` | `60` | Seconds the breaker stays open before re-probing |

### Per-key budgets & rate limits

Give each `PROXY_API_KEYS` entry a ceiling so the router is safe to share with a team. These
env vars are **global defaults**; set per-key overrides in `auth.json` with `hr limit set`.
`0` = unlimited (the default — no enforcement). Live usage shows in `/v1/status` and `hr status`.

| Variable | Default | Purpose |
|---|---|---|
| `PROXY_LIMIT_RPM` | `0` | Requests/minute per key (rolling 60s window) |
| `PROXY_LIMIT_REQ_DAY` | `0` | Requests per UTC day, per key |
| `PROXY_LIMIT_TOKENS_DAY` | `0` | Tokens per UTC day, per key |
| `PROXY_LIMIT_COST_DAY` | `0` | Estimated USD cost per UTC day, per key (see [Cost awareness](#cost--spend-awareness)) |

```bash
hr limit set sk-team-1 --rpm 60 --req-day 500 --tokens-day 100000 --cost-day 5   # per-key, written to auth.json
hr limit list                                                                    # show all
hr restart                                                                       # apply
```

Exceeding a limit returns `429` with a clear message and a `Retry-After` header. Per-key limits
in `auth.json` look like:

```json
{ "proxy_keys": { "sk-team-1": { "rpm": 60, "req_per_day": 500, "tokens_per_day": 100000, "cost_per_day": 5 } } }
```

### Adaptive upstream rate limiter

hermes-router automatically discovers and tracks each upstream provider's rate limits.
It starts from conservative built-in defaults and adjusts caps up or down based on
`x-ratelimit-*` response headers and 429 signals. Learned limits persist across
restarts in `rate_limits_state.json`.

> Two scopes are tracked per key, and each always maintains the full **`[R,T]×[M,H,D,W,Mo]`** grid (ten buckets). **Model** groups are authoritative (learn from `x-ratelimit-*` headers and hard 429 cuts; `Retry-After` holds that model only). Header-synced caps stay pinned (no success AIMD nudge until a non-header 429). Header applies use request-start observation time so stale responses cannot overwrite newer state. Failed upstream calls after admit release the R+T reservation on both scopes. New buckets start at `RATE_BUCKET_INITIAL_FILL` of cap (default `0.5`). Caps are initialized from built-in defaults, env overrides, or `auth.json`: **explicit values win**; any missing window is scaled linearly from minute (`Cap(W)=Cap(M)×(T_W/T_M)`); ordering stays **Mo≥…≥M** with explicit values sticky. Long windows are **never auto-deactivated** — all ten buckets stay binding for debit and ranking. **Provider-wide** groups are a shared-ceiling estimate (debited by all models on the key; softer 429 cuts; faster success recovery; never overwritten by response headers). When a provider-wide group is first created, its caps start at **10×** the model/base defaults for that provider (`RATE_PROVIDER_CAP_MULTIPLIER`, default `10`). Each request debits the **same absolute amount** from both scopes, so headroom **percentages** diverge because the caps differ. On a 429, if model headroom was ≥ 90% before the attempt, provider-wide gets one extra soft cut (surprise path, at most once per 60 s per provider-wide group). Provider-wide 429s also apply a tiny soft tick **`ε × (T_M/T_window)`** to every PW bucket (`RATE_LEARN_PW_TICK_EPS`, default `0.05`); longer windows move less per tick but accumulate. Success AIMD nudges apply to **minute** buckets only — H/D/W/Mo are stable priors adjusted by 429 evidence and PW ticks. `RATE_HEADROOM_THRESHOLD` is a ranking / “thin headroom” log signal only — **not** a hard skip before attempting. Persisted provider-wide caps are loaded as-is and are **not** re-multiplied on restart.

| Env var | Default | Description |
|---|---|---|
| `RATE_STATE_FILE` | `./rate_limits_state.json` | Path to learned-limits state file |
| `RATE_SHORT_WAIT_MS` | `500` | Max ms to sleep when a bucket is nearly empty before failing over |
| `RATE_HEADROOM_THRESHOLD` | `0.05` | Fraction of cap below which headroom is logged as “thin” (ranking signal only — not a hard skip) |
| `RATE_LEARN_PW_TICK_EPS` | `0.05` | Provider-wide soft-tick fraction at the minute window; scaled per bucket as `ε × (T_M/T_window)` on every PW 429 |
| `RATE_PROVIDER_CAP_MULTIPLIER` | `10` | Multiplier applied to base caps when creating a new provider-wide TBF group |
| `RATE_BUCKET_INITIAL_FILL` | `0.5` | Fraction of cap for new buckets when tokens are not set explicitly |
| `RATE_LEARN_SUCCESS_STREAK` | `20` | Consecutive successes before nudging a cap up |
| `RATE_LEARN_NUDGE_PCT` | `5` | Percent to increase cap on a success streak |
| `RATE_LEARN_CUT_FACTOR` | `0.8` | Multiplier applied to observed rate on 429 |
| `RATE_LEARN_CUT_FACTOR_PROVIDER` | `0.95` | Provider-wide soft cut vs observed rate on 429 |
| `RATE_LEARN_SOFT_CUT_FACTOR` | `0.9` | Provider-wide soft cut vs current cap when history is thin |
| `RATE_LEARN_SUCCESS_STREAK_PROVIDER` | `10` | Consecutive successes before nudging a provider-wide cap up |
| `RATE_LEARN_NUDGE_PCT_PROVIDER` | `8` | Percent to increase a provider-wide cap on a success streak |
| `RATE_STATE_FLUSH_INTERVAL` | `600` | Seconds between background state flushes |
| `RATE_DEFAULT_<PROVIDER>_<WINDOW>` | — | Override built-in default cap (e.g. `RATE_DEFAULT_GROQ_RPM=60`) |
| `RATE_BUCKET_CSV_ENABLED` | off | When `1`/`true`/`yes`, append TBF cap-change events to a CSV |
| `RATE_BUCKET_CSV` | `./rate_bucket_events.csv` | Path for that append-only event log (Calc/Excel-friendly) |

The rate limit state is visible on the dashboard **Providers** page (provider-wide token
buckets + Rate headroom column) and **Models** page (combined capabilities + per-model headroom /
limiting factor; click a row for bucket bars), and in
`/v1/status` under each provider's `rate_limits` key. For local development,
enable `RATE_BUCKET_CSV_ENABLED` to append each cap change (nudge/cut/header
pin/lift) with headroom into a CSV you can open in LibreOffice Calc or Excel;
the file always appends across restarts.

### Adaptive per-model token caps

hermes-router tracks effective input/output token ceilings per `(provider, model)`.
It seeds from `/models` metadata when available and tightens from classified
413 / token-limit 400 responses (gentle raises on near-cap successes).

| Var | Default | Notes |
|---|---|---|
| `TOKEN_CAPS` | `1` | `0` disables adaptive caps (static env/defaults only) |
| `TOKEN_CAPS_STATE_FILE` | `./token_caps_state.json` | Persisted learned/metadata caps |

`{PROVIDER}_SKIP_TOKENS_OVER` and `{PROVIDER}_MAX_OUTPUT_TOKENS` remain provider-wide
outer fences — learned values may only tighten further inside them. Caps appear under
each provider in `/v1/status` as `token_caps`.

### Cost / spend awareness

The router estimates **spend** from a built-in price table (USD per 1M tokens, input/output).
Free providers and subscription plans (Codex, Kimi coding) are **$0**. Estimated cost shows per
provider and per key in `/v1/usage`, `/v1/status`, `hr status`, the VS Code dashboard, and
`/metrics` (`hermes_router_cost_usd_total`). USD is always the canonical figure.

| Variable | Default | Purpose |
|---|---|---|
| `COST_CURRENCY` | `USD` | A second currency to *also* display (e.g. `INR`) — requires `COST_FX_RATE` |
| `COST_FX_RATE` | `0` | USD→`COST_CURRENCY` multiplier (e.g. `83`); `0` shows USD only |
| `MODEL_PRICES_FILE` | *(unset)* | JSON file of price overrides — `{"model-substr": [input, output]}` (USD per 1M tokens) — merged over the built-in table |

Prices are **best-effort estimates** and drift over time; correct them with `MODEL_PRICES_FILE`.

### Local model (Ollama / LM Studio / llama.cpp)

Set either of the first two to enable a `local` provider pointing at a model on your own
machine. It's keyless (cloud providers remain the fallback). See
[Providers → Local models](/providers/#local-models-ollama--lm-studio--llamacpp).

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_BASE_URL` | `http://localhost:11434/v1` | Your local server's OpenAI-compatible endpoint (LM Studio: `:1234/v1`) |
| `LOCAL_MODEL` | `llama3.1` | Local model id (comma-separate for multi-model failover) |
| `LOCAL_API_KEY` | `local` | Only if your local server actually requires a key |
| `LOCAL_EMBED_MODEL` | *(unset)* | Optional — also serve `/v1/embeddings` from the local server |

> Send model `hermes-router:fast` (or header `X-Hermes-Profile: fast`) to prefer the local model
> for short/casual turns, with cloud fallback for heavier requests.

### Per-provider model

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Model override (set via `hr model set`) |
| `CODEX_MODEL` | `gpt-5.5` | Codex (ChatGPT subscription) model — see [providers.md](/providers/) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model override (set via `hr model set`) |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Model override (set via `hr model set`) |
| `<PROVIDER>_MODEL` | *(varies)* | Same pattern applies to all providers |

### Per-provider embeddings

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Embedding model (empty disables this provider for `/v1/embeddings`) |
| `<PROVIDER>_EMBED_MODEL` | *(gemini/mistral/cohere set)* | Same pattern for embeddings; set empty to disable |

### Per-provider capability overrides

The router auto-probes each provider at startup, but you can force the result:

| Variable | Default | Purpose |
|---|---|---|
| `<PROVIDER>_SUPPORTS_TOOLS` | *(auto-probed)* | Force tool-capability on/off (`1`/`0`) |
| `<PROVIDER>_REASONING` | *(auto-probed)* | Force reasoning-model on/off (`1`/`0`) |
| `<PROVIDER>_SKIP_TOKENS_OVER` | *(per provider)* | Skip this provider when an estimated request exceeds this many tokens (`0` = never). With `TOKEN_CAPS=1`, this is an outer fence — per-model learned caps may only tighten further. |
| `<PROVIDER>_MAX_OUTPUT_TOKENS` | *(per provider)* | Clamp `max_tokens` down to this provider's output ceiling (`0` = no clamp). With `TOKEN_CAPS=1`, this is an outer fence — per-model learned caps may only tighten further. |

## Model overrides

Switch models without editing files:

```bash
hr model list                              # see all providers and their active model
hr model set anthropic claude-sonnet-4-6   # upgrade Anthropic to Sonnet
hr model set openai gpt-4o                 # use full GPT-4o instead of mini
hr model set gemini gemini-2.5-pro         # switch Gemini to Pro
hr restart                                 # apply changes
```

Overrides are stored as plain variables in `.env` (e.g. `ANTHROPIC_MODEL=claude-sonnet-4-6`)
and shown in `hr model list`. Use `hr model set` (not the dashboard) to change overrides.

### Multiple models per provider

A provider can use **several models** — just give `<PROVIDER>_MODEL` a comma-separated list:

```bash
hr model set gemini gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.0-flash
hr restart
```

Free-tier rate limits are almost always **per-model**, so each model is its own quota
bucket. When the first model hits its limit (429), the router **fails over to the next model
on the same key** before cascading to the next provider — multiplying free throughput along a
new axis (keys × models × providers), with no extra signups. Each model is **also a first-class
routing candidate**, scored on its own rating and capability — so the router can pick the right
model in the list for each request (e.g. a stronger model for a hard or tool-using turn), not just
fall over to it. Within equal cost/capability buckets, the router prefers known stronger model
families, then falls back to your listed order.

> **Mixing model classes is fine.** Tool-calling and reasoning are detected **per model** at
> startup, so you can safely list models of different classes (e.g.
> `gemini-2.5-flash-lite,gemini-2.5-pro`) — each is routed and gated on its own capability. Force a
> result per model with `<PROVIDER>_<MODEL>_SUPPORTS_TOOLS` / `_REASONING` (model id upper-cased,
> non-alphanumerics → `_`, e.g. `GEMINI_GEMINI_2_5_PRO_SUPPORTS_TOOLS=1`); the provider-wide
> `<PROVIDER>_SUPPORTS_TOOLS` / `_REASONING` still applies as the default for all its models.

### Auto model discovery

Enable `AUTO_DISCOVER_MODELS=1` (or `hr features enable model_discovery`) to have the
router query configured providers' OpenAI-compatible `/models` endpoints at startup. It
keeps the configured models that still exist, appends the best discovered models up to
`AUTO_DISCOVER_MODEL_LIMIT`, and updates the in-memory routing pool for that run.

This is opt-in because some gateways expose paid or very large catalogs. Known mixed
free/paid gateways are filtered to free model ids where possible, and very large/special
providers such as Hugging Face are skipped unless you opt in per provider with
`HUGGINGFACE_AUTO_DISCOVER_MODELS=1`.

### Filter specialized models from discovery

Enable `FILTER_SPECIALIZED_MODELS=1` (or `hr features enable filter_specialized_models`)
to drop purpose-specific models from discovered `/models` catalogs: TTS, STT/Whisper,
image generation, OCR, video, embeddings, moderation, and rerank. Detection uses
catalog metadata when present, otherwise name-pattern matching.

Configured `{PROVIDER}_MODEL` lists are never filtered — put a specialized ID there
explicitly if you want it in rotation. Embedding routing via `{PROVIDER}_EMBED_MODEL`
is unchanged.

### Per-provider exclude list

To permanently block specific model IDs for a provider — even when listed in
`{PROVIDER}_MODEL` or re-added by auto-discovery — set `{PROVIDER}_EXCLUDE_MODELS`:

```bash
OPENROUTER_EXCLUDE_MODELS=some/unwanted-model:free
MISTRAL_EXCLUDE_MODELS=mistral-tiny
SAMBANOVA_EXCLUDE_MODELS=gemma-4-31B-it
```

Excluded models are matched case-insensitively (exact ID only, no globs). The
filter applies both to your configured model list and to any extras appended by
auto-discovery. If every model for a provider is excluded, the provider stays in
rotation with no usable models and a warning is logged at startup.

## Key selection

When a provider holds several keys (or several accounts), the router uses **sticky-until-fail**
selection: it keeps using the same key for a `(provider, model)` until that key errors or is
rate-limited, then tries the next ready key in stable deque order. There is no round-robin or
sequential rotation mode.

The legacy `ROTATION_MODE` env var (and `hr mode`) are **ignored** — if set, the router logs a
warning at startup. Failover, per-key cooldowns, and the circuit breaker keep working as before.
Key usage counts show in `hr status`, `/v1/status` (`keys[].requests`), and the web dashboard.

---

**Next:** [Usage](/usage/) — call the router from the OpenAI or Anthropic SDK (tool calling, streaming, embeddings).
