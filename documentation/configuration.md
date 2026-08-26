# Configuration

All configuration is via environment variables (in `.env`) and the `auth.json` credential
store. Everything is optional with sensible defaults — the proxy runs out of the box once
it has at least one key.

## Core features vs. add-ons

hermes-router splits its behavior into two groups:

- **Core features** — always on; they *are* the proxy. Auth, the credential pool + key affinity
  selection, fallback, the circuit breaker, catalog selection, protocol translation
  (OpenAI/Anthropic-provider/Codex outbound), feature probing, token counting, request guardrails, and
  usage/cost tracking.
- **Add-ons** — optional behaviors you turn on when you want them. Each is backed by an
  environment variable (or some `auth.json` config), and unset = off (except the response
  cache, on by default).

### `hr features` — see and toggle add-ons

```bash
hr features list                      # core features + every add-on, with on/off
hr features enable semantic_cache     # writes the backing var to .env
hr features disable semantic_cache
hr restart                            # apply
```

| Add-on | Backing setting | Default | What it does |
|---|---|---|---|
| `response_cache` | `CACHE_TTL_SECONDS` | **on** | Serve identical requests from an in-memory TTL+LRU cache |
| `semantic_cache` | `SEMANTIC_CACHE` | off | Also serve cached answers for *similar* prompts |
| `fast_routing` | `FAST_ROUTE_THRESHOLD` | off | Short requests prefer low-latency providers on ties |
| `model_discovery` | `AUTO_DISCOVER_MODELS` | off | Refresh provider model lists from `/models` at startup |
| `filter_specialized_models` | `FILTER_SPECIALIZED_MODELS` | off | Drop TTS/STT/image-gen/OCR/embedding/moderation/rerank IDs; keep multimodal chat (text in+out) |
| `token_caps` | `TOKEN_CAPS` | **on** | Adaptive per-model input/output ceilings |
| `key_budgets` | `auth.json` / `PROXY_LIMIT_*` | off | Per-key RPM / daily request / token / cost ceilings — manage with `hr limit` |
| `local_model` | `LOCAL_BASE_URL` / `LOCAL_MODEL` | off | Route to a model on your own machine — manage with `hr model set local` |
| `request_log` | `REQUEST_LOG_SIZE` | **on** | In-memory ring buffer of recent requests |
| `dashboard` | — | **on** | Browser UI at `/dashboard` |
| `dashboard_open` | `DASHBOARD_OPEN` | off | Dashboard + monitoring/config APIs without an access key (chat stays keyed) |

`hr features enable/disable` toggles the simple **flag** add-ons by writing their variable to
`.env`. Config-kind add-ons show status and point you to the command that manages them. The live
state is also in `/v1/status` under `features`.

## Where your keys live

`hr auth add` writes to **`auth.json`** — the router's own credential store, kept next to
the router. It's git-ignored, so real keys are never committed. Codex (ChatGPT
subscription) logins are stored separately under `codex_accounts` (via
`hr auth import-codex`); the proxy refreshes their OAuth access tokens automatically.

```json
{
  "providers": {
    "openrouter": ["sk-or-key1", "sk-or-key2"],
    "gemini": ["AIzaSy-key"]
  }
}
```

> Keys in `.env` (e.g. `OPENROUTER_API_KEYS=k1,k2`) still work too — the proxy reads
> `auth.json` first, then falls back to `.env`. Point at a different file with
> `ROUTER_AUTH_FILE=/path/to/auth.json`.

## Settings (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8319` | Port to listen on |
| `HOST` | `0.0.0.0` | Bind address. Set `127.0.0.1` to listen on localhost only (recommended on a shared/VPS host — reach it via localhost or an SSH tunnel). |
| `PROXY_API_KEYS` | *(auto-generated)* | Comma-separated keys your app uses to authenticate — and, unless `DASHBOARD_OPEN=1`, the key needed to open the web dashboard. If left unset (or on the `.env.example` placeholder), the proxy generates a real random key on first boot and saves it back to `.env`, logging it once. Add more from the dashboard's **Access Keys** page, or set your own here. |
| `ROUTER_AUTH_FILE` | `./auth.json` | Where keys are stored |
| `CACHE_TTL_SECONDS` | `300` | Response cache lifetime (`0` disables). Entries are namespaced per API key, so different `PROXY_API_KEYS` never share a cached answer — safe for multi-tenant use |
| `SESSION_AFFINITY_TTL_SECONDS` | `300` | Idle lifetime for session affinity (sliding; refreshes on each successful turn). Aligns with typical upstream prompt-cache windows. `0` = no idle expiry (cascade/clear still drop affinity). Independent of `CACHE_TTL_SECONDS` |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `REASONING_TOKEN_RESERVE` | `4096` | Extra output budget added for reasoning models so hidden chain-of-thought doesn't eat the answer (`0` disables) |

### Advanced settings

Sensible defaults — most users never touch these.

| Variable | Default | Purpose |
|---|---|---|
| `MAX_REQUEST_BYTES` | `10485760` (10 MB) | Max request body size; larger requests get `413` (guards against memory exhaustion) |
| `WORKER_THREADS` | `16` | Waitress worker threads (concurrency). The HTTP connection pool scales with this |
| `CACHE_MAX_SIZE` | `100` | Max entries in the response cache (LRU eviction) |
| `SEMANTIC_CACHE` | `0` | If `1`, also serve cached answers for *similar* prompts (needs an embedding provider; falls back to exact match otherwise) |
| `SEMANTIC_CACHE_THRESHOLD` | `0.95` | Cosine-similarity cutoff for a semantic hit (`1.0` = identical; lower = looser matching) |
| `FAST_ROUTE_THRESHOLD` | `0` | If >0, requests under this many tokens prefer low-latency providers first (`0` disables) |
| `AUTO_DISCOVER_MODELS` | `0` | If `1`, fetch configured providers' `/models` lists at startup, prune listed models that disappeared, and append the best discovered models |
| `AUTO_DISCOVER_MODEL_LIMIT` | `8` | Max models kept per provider when `AUTO_DISCOVER_MODELS=1` |
| `FILTER_SPECIALIZED_MODELS` | `0` | If `1`, drop purpose-specific models from auto-discovery catalogs (configured `{PROVIDER}_MODEL` lists are never filtered) |
| `DASHBOARD_OPEN` | `0` | If `1`, dashboard + monitoring/config APIs work without an access key (chat/embeddings/models stay keyed). Prefer `HOST=127.0.0.1` — exposes admin config to anyone who can reach the port |
| `UNSUITABLE_MODEL_BASE_S` | `60` | Initial cool-down seconds after a model-unsuitable 404/400 |
| `UNSUITABLE_MODEL_CAP_S` | `3600` | Max cool-down seconds (exponential backoff caps here) |
| `{PROVIDER}_EXCLUDE_MODELS` | — | Comma-separated model IDs to block for a provider (case-insensitive). Excluded models are stripped from config and discovery. Dashboard Models page can Block/Unblock the same list without restart. |
| `ROUTER_MODEL_ID` | `hermes-router` | The model name clients send (the proxy maps it to each provider's real model) |
| `ROUTER_STATE_FILE` | `./router_state.json` | Where provider ratings/capabilities are cached between restarts |
| `ROUTER_STATE_TTL_HOURS` | `24` | How long the cached probe state is trusted before re-probing (`0` = re-probe every start) |
| `BREAKER_WINDOW` | `8` | Recent outcomes the circuit breaker weighs per provider |
| `BREAKER_MIN_SAMPLES` | `4` | Minimum samples before the breaker can trip |
| `BREAKER_ERROR_RATE` | `0.5` | Health-failure fraction that trips the breaker |
| `BREAKER_COOLDOWN` | `60` | Seconds the breaker stays open before re-probing |
| `TTFT_ABORT_ENABLED` | `1` | If `1`, abort attempts whose first-byte wait exceeds the per-candidate TTFT deadline and cascade |
| `TTFT_FLOOR_S` | `5.0` | Warm deadline never below this many seconds |
| `TTFT_MULT` | `3.0` | Warm deadline = `max(floor, mult × EWMA TTFT)` |
| `TTFT_MIN_SAMPLES` | `5` | Successful TTFT samples before the relative deadline applies |
| `TTFT_COLD_DEADLINE_S` | `20.0` | Absolute first-byte wait while a candidate is still cold |
| `TTFT_EWMA_ALPHA` | `0.2` | EWMA smoothing for successful TTFT samples |

### Per-key budgets & rate limits

Give each `PROXY_API_KEYS` entry a ceiling so the proxy is safe to share with a team. These
env vars are **global defaults**; set per-key overrides in `auth.json` with `hr limit set`.
`0` = unlimited (the default — no enforcement). Live usage shows in `/v1/status` and `hr status`.

| Variable | Default | Purpose |
|---|---|---|
| `PROXY_LIMIT_RPM` | `0` | Requests/minute per key (rolling 60s window) |
| `PROXY_LIMIT_REQ_DAY` | `0` | Requests per UTC day, per key |
| `PROXY_LIMIT_TOKENS_DAY` | `0` | Tokens per UTC day, per key |
| `PROXY_LIMIT_COST_DAY` | `0` | Estimated USD cost per UTC day, per key (see Cost awareness below) |

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

> Two scopes are tracked per key, and each always maintains the full **`[R,T]×[M,H,D,W,Mo]`** grid (ten buckets). **Model** groups are authoritative (learn from `x-ratelimit-*` headers and hard 429 cuts; `Retry-After` holds that model only). Header-synced caps stay pinned (no success AIMD nudge until a non-header 429). Header applies use request-start observation time so stale responses cannot overwrite newer state. Failed upstream calls after admit release the R+T reservation on both scopes. New buckets start at `RATE_BUCKET_INITIAL_FILL` of cap (default `0.5`). Caps are initialized from built-in defaults, env overrides, or `auth.json`: **explicit values win**; any missing window is scaled linearly from minute (`Cap(W)=Cap(M)×(T_W/T_M)`); ordering stays **Mo≥…≥M** with explicit values sticky. Long windows are **never auto-deactivated** — all ten buckets stay on for debit and ranking. **Selection never hard-skips on empty rate-limit estimates** (explore-into-limit / force-admit); headroom only ranks new-session and post-bump picks. Optional sleep only when refill wait is **&lt; `RATE_ADMIT_WAIT_S`** (default 60s); `Retry-After` still holds and cascades. Hard model 429s cut **one** ladder-attributed bucket, then **reclamp** so adjacent windows satisfy `C_short ≤ C_long ≤ C_short×(T_long/T_short)`. Minute windows nudge on the normal success streak; longer windows nudge slower (`RATE_LEARN_LONG_*`). **Provider-wide** groups are a shared-ceiling estimate (debited by all models on the key; softer 429 cuts; faster success recovery; never overwritten by response headers). When a provider-wide group is first created, its caps start at **10×** the model/base defaults for that provider (`RATE_PROVIDER_CAP_MULTIPLIER`, default `10`). Each request debits the **same absolute amount** from both scopes, so headroom **percentages** diverge because the caps differ. On a 429, if model headroom was ≥ 90% before the attempt, provider-wide gets one extra soft cut (surprise path, at most once per 60 s per provider-wide group). Provider-wide 429s also apply a tiny soft tick **`ε × (T_M/T_window)`** to every PW bucket (`RATE_LEARN_PW_TICK_EPS`, default `0.05`); longer windows move less per tick but accumulate. `RATE_HEADROOM_THRESHOLD` is a ranking / “thin headroom” log signal only — **not** a hard skip before attempting. Persisted provider-wide caps are loaded as-is and are **not** re-multiplied on restart.

| Env var | Default | Description |
|---|---|---|
| `RATE_STATE_FILE` | `./rate_limits_state.json` | Path to learned-limits state file |
| `RATE_ADMIT_WAIT_S` | `60` | Max seconds to sleep for a thin bucket before force-admit (explore) |
| `RATE_EXHAUSTED_WAIT_S` | `60` | After a full cascade miss, max seconds to wait for the shortest rate hold or key cool-down before one exhausted retry (also probes circuit-open providers) |
| `RATE_HEADROOM_THRESHOLD` | `0.05` | Fraction of cap below which headroom is logged as “thin” (ranking signal only — not a hard skip) |
| `RATE_LEARN_CLEAR_HEADROOM` | `0.5` | Ladder 429: shorter window treated as clear (not violated) at/above this headroom |
| `RATE_LEARN_LONG_STREAK` | `40` | Successes before nudging H/D/W/Mo caps |
| `RATE_LEARN_LONG_NUDGE_PCT` | `2` | Percent nudge for long-window success streaks |
| `RATE_LEARN_PW_TICK_EPS` | `0.05` | Provider-wide soft-tick fraction at the minute window; scaled per bucket as `ε × (T_M/T_window)` on every PW 429 |
| `RATE_PROVIDER_CAP_MULTIPLIER` | `10` | Multiplier applied to base caps when creating a new provider-wide rate-limit group |
| `RATE_BUCKET_INITIAL_FILL` | `0.5` | Fraction of cap for new buckets when tokens are not set explicitly |
| `RATE_LEARN_SUCCESS_STREAK` | `20` | Consecutive successes before nudging a minute-window cap up |
| `RATE_LEARN_NUDGE_PCT` | `5` | Percent to increase minute-window cap on a success streak |
| `RATE_LEARN_CUT_FACTOR` | `0.8` | Multiplier applied to observed rate on 429 |
| `RATE_LEARN_CUT_FACTOR_PROVIDER` | `0.95` | Provider-wide soft cut vs observed rate on 429 |
| `RATE_LEARN_SOFT_CUT_FACTOR` | `0.9` | Provider-wide soft cut vs current cap when history is thin |
| `RATE_LEARN_SUCCESS_STREAK_PROVIDER` | `10` | Consecutive successes before nudging a provider-wide cap up |
| `RATE_LEARN_NUDGE_PCT_PROVIDER` | `8` | Percent to increase a provider-wide cap on a success streak |
| `RATE_STATE_FLUSH_S` | `600` | Seconds between background state flushes |
| `RATE_DEFAULT_<PROVIDER>_<WINDOW>` | — | Override built-in default cap (e.g. `RATE_DEFAULT_GROQ_RPM=60`) |
| `RATE_BUCKET_CSV_ENABLED` | off | When `1`/`true`/`yes`, append rate-limit cap-change events to a CSV |
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
413 / token-limit 400 responses (gentle raises on near-cap successes). Each side
has an explicit **confidence** (`input_confidence` / `output_confidence`, 0–1).
Routing **hard-skips** a learned cap only when confidence ≥ `TOKEN_CAPS_HARD_CONFIDENCE`
(default `0.7`); low-confidence guesses are explorable so a real limit error can
raise confidence. `{PROVIDER}_SKIP_TOKENS_OVER` / `{PROVIDER}_MAX_OUTPUT_TOKENS`
remain always-on outer fences.

| Var | Default | Notes |
|---|---|---|
| `TOKEN_CAPS` | `1` | `0` disables adaptive caps (static env/defaults only) |
| `TOKEN_CAPS_STATE_FILE` | `./token_caps_state.json` | Persisted learned/metadata caps |
| `TOKEN_CAPS_HARD_CONFIDENCE` | `0.7` | Minimum confidence to hard-skip / hard-clamp on a learned cap |

Caps appear under each provider in `/v1/status` as `token_caps` (includes confidences).

### Cost / spend awareness

The proxy estimates **spend** from a built-in price table (USD per 1M tokens, input/output).
Free providers and subscription plans (Codex, Kimi coding) are **$0**. Estimated cost shows per
provider and per key in `/v1/usage`, `/v1/status`, `hr status`, and the web dashboard (USD).

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_PRICES_FILE` | *(unset)* | JSON file of price overrides — `{"model-substr": [input, output]}` (USD per 1M tokens) — merged over the built-in table |

Prices are **best-effort estimates** and drift over time; correct them with `MODEL_PRICES_FILE`.

### Local model (Ollama / LM Studio / llama.cpp)

Set either of the first two to enable a `local` provider pointing at a model on your own
machine. It's keyless (cloud providers remain the fallback). See
[providers.md → Local models](providers.md).

| Variable | Default | Purpose |
|---|---|---|
| `LOCAL_BASE_URL` | `http://localhost:11434/v1` | Your local server's OpenAI-compatible endpoint (LM Studio: `:1234/v1`) |
| `LOCAL_MODEL` | `llama3.1` | Local model id (comma-separate for multi-model fallback) |
| `LOCAL_API_KEY` | `local` | Only if your local server actually requires a key |
| `LOCAL_EMBED_MODEL` | *(unset)* | Optional — also serve `/v1/embeddings` from the local server |

> Send model `hermes-router:fast` (or header `X-Hermes-Profile: fast`) to prefer the local model
> for short/casual turns, with cloud fallback for heavier requests.

### Per-provider model

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Model override (set via `hr model set`) |
| `CODEX_MODEL` | `gpt-5.5` | Codex (ChatGPT subscription) model — see [providers.md](providers.md) |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model override (set via `hr model set`) |
| `GEMINI_MODEL` | `gemini-2.5-flash-lite` | Model override (set via `hr model set`) |
| `<PROVIDER>_MODEL` | *(varies)* | Same pattern applies to all providers |

### Per-provider embeddings

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Embedding model (empty disables this provider for `/v1/embeddings`) |
| `<PROVIDER>_EMBED_MODEL` | *(gemini/mistral/cohere set)* | Same pattern for embeddings; set empty to disable |

### Per-provider feature overrides

The proxy auto-probes each provider at startup, but you can force the result:

| Variable | Default | Purpose |
|---|---|---|
| `<PROVIDER>_SUPPORTS_TOOLS` | *(auto-probed)* | Force tool support on/off (`1`/`0`) |
| `<PROVIDER>_REASONING` | *(auto-probed)* | Force reasoning-model on/off (`1`/`0`) |
| `<PROVIDER>_SKIP_TOKENS_OVER` | *(per provider)* | Skip this provider when an estimated request exceeds this many tokens (`0` = never). With `TOKEN_CAPS=1`, this is an outer fence — per-model learned caps may only tighten further. |
| `<PROVIDER>_MAX_OUTPUT_TOKENS` | *(per provider)* | Clamp `max_tokens` down to this provider's output ceiling (`0` = no clamp). With `TOKEN_CAPS=1`, this is an outer fence — per-model learned caps may only tighten further. |

### General intelligence ranking

| Variable | Default | Purpose |
|---|---|---|
| `GI_RANKINGS_FILE` | `./gi_rankings.json` | Checked-in snapshot of default GI scores (0–100), plus optional `aliases` |
| `GI_OVERRIDES_FILE` | `./gi_overrides.json` | Dashboard overrides (set/clear from the Models modal) |

Rebuild the snapshot with `scripts/refresh_gi_rankings.py` from LMSYS Arena and Artificial
Analysis JSON exports (median of min–max-normalized scores). Matching uses normalized model
ids (strip `org/`, `:tag`, trailing `-free`/`_free`, quants), snapshot `aliases`, then longest
**contained** key (min key length 4) — a base id never inherits a longer `-lite`/`-flash` sibling.
Specialty modality tokens (`image`, `veo`, `live`, `omni`, `translate`, `computer-use`) block
inheriting a chat score; those SKUs stay **0** unless exact/aliased (GI is chat-Arena based).
The proxy hot-reloads `GI_RANKINGS_FILE` / `GI_OVERRIDES_FILE` when file mtime changes,
and **always** re-reads both on process start/restart. Snapshot scores are not stored in
`router_state.json` — only manual overrides in `GI_OVERRIDES_FILE` persist. Unknown models
score **0** until you assign an override.

```bash
python scripts/refresh_gi_rankings.py \
  --lmsys data/gi_sources/lmsys.json \
  --aa data/gi_sources/aa.json \
  --catalog data/gi_sources/catalog.json \
  --llm \
  --out gi_rankings.json
```

- `--catalog` — runtime catalog model ids; writes a `coverage` summary and **exits 1** if
  coverage is below 80%.
- `--llm` — propose aliases for unmatched catalog ids via an OpenAI-compatible endpoint
  (maintainer-only; not used by the proxy). Requires:

| Variable | Purpose |
|---|---|
| `GI_ALIAS_LLM_BASE_URL` | API base URL (e.g. `https://api.openai.com/v1`) |
| `GI_ALIAS_LLM_API_KEY` | Bearer token |
| `GI_ALIAS_LLM_MODEL` | Chat model id (default `gpt-4o-mini` if unset in script) |

See [`data/gi_sources/README.md`](../data/gi_sources/README.md) for how to obtain LMSYS / AA exports.

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
bucket. When the first model hits its limit (429), the proxy **falls back to the next model
on the same key** before cascading to the next provider — multiplying free throughput along a
new axis (keys × models × providers), with no extra signups. Each model is **also a first-class
routing candidate**, scored on its own GI and tool/reasoning support — so the proxy can pick the right
model in the list for each request (e.g. a stronger model for a hard or tool-using turn), not just
fall over to it. Within equal cost, lower GI overshoot wins; listed order breaks remaining ties.

> **Mixing model classes is fine.** Tool-calling and reasoning are detected **per model** at
> startup, so you can safely list models of different classes (e.g.
> `gemini-2.5-flash-lite,gemini-2.5-pro`) — each is routed and gated on its own GI and features. Force a
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
image generation, OCR, embeddings, moderation, and rerank. Multimodal **chat** models
that accept extra inputs (image / video / audio) but still have **text on both input and
output** (e.g. `text+image+video->text`) are kept. Detection uses catalog metadata when
present, otherwise name-pattern matching.

Configured `{PROVIDER}_MODEL` lists are never filtered — put a specialized ID there
explicitly if you want it in rotation. Embedding routing via `{PROVIDER}_EMBED_MODEL`
is unchanged.

With a large `AUTO_DISCOVER_MODEL_LIMIT`, enable this filter so discovery does not
flood the chat roster with imagen / audio-out / embedding / deep-research-style IDs.
For configured junk that slips through, use `{PROVIDER}_EXCLUDE_MODELS`.

### Unsuitable-model cooldown

When a chat candidate returns **404**, or a **400** whose body looks like
model-not-found / unknown model / not supported for this endpoint, the proxy cools
that `(provider, model)` in memory with exponential backoff (default base 60s, cap 1h;
override with `UNSUITABLE_MODEL_BASE_S` / `UNSUITABLE_MODEL_CAP_S`). Later requests skip
it until the cool-down expires; a success clears the streak. Payload-shaped 400s
(e.g. missing `reasoning_content`) cascade for that request only and do **not** cool.
**429** stays on the token-bucket path and is unaffected. Cooldowns are not persisted
across restarts.

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

From the dashboard **Models** page you can also **Block** a model in its detail
modal (or **Unblock** from the Blocked models panel). That writes the same
`{PROVIDER}_EXCLUDE_MODELS` line in `.env` and updates the live roster immediately
— no restart required. Hand-edited excludes show in the Blocked panel too.

## Key selection

When a provider holds several keys (or several accounts), the proxy uses **key affinity**
selection: it keeps using the same key for a `(provider, model)` until that key errors or is
rate-limited, then tries the next ready key in stable deque order. There is no round-robin or
sequential rotation mode.

The legacy `ROTATION_MODE` env var (and `hr mode`) are **ignored** — if set, the proxy logs a
warning at startup. Fallback, per-key cooldowns, and the circuit breaker keep working as before.
Key usage counts show in `hr status`, `/v1/status` (`keys[].requests`), and the web dashboard.
