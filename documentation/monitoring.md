# Monitoring

## Web dashboard

The proxy serves a built-in **browser dashboard** — no install, no extra service. It ships
inside `router.py`, so it's available the moment the proxy is running. Just open the router
in a browser:

```
http://localhost:8319/          # redirects to the dashboard
http://localhost:8319/dashboard
```

On first load it asks for your **access key** (one of `PROXY_API_KEYS` — see
[below](#proxy-api-keys--the-dashboard-key) if you don't have one yet) and remembers it in the
browser's local storage. It's a full control panel, not just a read-only view — a left sidebar
splits it into pages, refreshed every 5 seconds:

- **Overview** — a plain-language status card, the endpoint/model to point your client at, a
  setup checklist, and summary stats (requests, tokens, spend, cache hit-rate, error rate)
- **Providers** — health cards (worst-first) plus a detailed table (GI, latency, keys,
  breaker state, cost, rate headroom), and a **provider-wide rate headroom** panel for the
  adaptive upstream rate-limit headroom
- **Provider Keys** — add a key for any provider and see live per-key request counts and daily
  budget usage
- **Access Keys** — mint new `PROXY_API_KEYS` for teammates/other apps, with optional
  budget limits, and revoke them — see
  [below](#proxy-api-keys--the-dashboard-key)
- **Models** — one row per configured model (capabilities, key status dots, limiting factor,
  headroom); click a row for stacked per-key rate-limit bucket bars
- **Add-ons** — toggle optional features on/off, plus live cache stats
- **Request Log** — the last requests (endpoint, provider, model, latency, complexity score,
  Fail/Skip cascade counts, tokens, status), filterable by status and endpoint; click Fail/Skip
  to open the full cascade path (skipped / failed / success) with reasons


Every write (add a key, toggle an add-on, mint/revoke an access key) shows a
"Restart Required" banner — click it to restart the proxy in place; the page reconnects
automatically once it's back.

It's pure HTML/JS (no framework, no external CDN) and reads/writes only the router's own
`/v1/*` endpoints — so it adds essentially no memory or CPU to the proxy itself.

> **Accessing it remotely.** By default the proxy binds to `0.0.0.0` (all interfaces). If you
> set `HOST=127.0.0.1` (localhost-only, recommended on a shared/VPS host), reach the dashboard
> over an SSH tunnel: `ssh -L 8319:127.0.0.1:8319 user@server`, then open
> `http://localhost:8319/` locally. The raw API endpoints stay key-protected either way.

## Proxy API keys & the dashboard key

`PROXY_API_KEYS` is the credential your app uses to call the proxy **and** the key that unlocks
the web dashboard — there's only one tier, no separate "admin" vs. "chat" key. If you never set
one, the proxy generates a real random key on first boot and saves it to `.env`, logging it once
so you can copy it (this also replaces the placeholder value `.env.example` ships with, so
copying that file verbatim doesn't leave every install on the same public default). Once you have
one real key, it's left alone — nothing regenerates it out from under you.

**Adding more keys** — for a teammate, a CI pipeline, or another app — is done from the
dashboard's **Access Keys** page: give it an optional name and optional limits (requests/min,
requests/day, tokens/day, cost/day — blank means unlimited), and it generates a new key. **The
full key is shown exactly once**, in a copy box — after that, only its last 6 characters are ever
displayed again, matching every other key in this project. A key needs a restart (the dashboard
prompts for one) before it can actually authenticate.

Existing access keys can have their name/limits edited in place, or be revoked — revoking removes
it from `PROXY_API_KEYS` so it can no longer authenticate. You can't revoke the last remaining
key; that would lock out the dashboard itself.

## Terminal dashboard (`hr status`)

`hr status` prints a live, per-provider dashboard — rating, health (circuit-breaker
state), key pool, latency, and cache stats — without needing curl or an API key:

```bash
hr status
hr status --json   # raw JSON for scripts
```

## Usage analytics (`/v1/usage`)

`GET /v1/usage` (access key required) returns a JSON summary for dashboards and billing:

- **per provider** — requests, errors, tokens served, and estimated `cost` (`{"usd": …}`)
- **per key** — request, token, and cost totals (lifetime + today, plus the live RPM window);
  keys are shown by their **last 6 chars only**, never in full
- **cache** — hits, misses, hit-rate, semantic hits
- **totals** — total tokens, total estimated cost, and uptime

Cost is estimated from a built-in price table; free providers and subscription plans are `$0`.
See [Configuration → Cost awareness](configuration.md).

```bash
curl -H "Authorization: Bearer sk-router-1" http://localhost:8319/v1/usage
```

## JSON status (`/v1/status`)

`GET /v1/status` (access key required) returns the full picture as JSON: per-provider key
cooldown state, rating, model, latency, `supports_tools`, `reasoning`, tokens served,
circuit-breaker status, plus cache (incl. semantic), routing, and per-key
limit/usage config. This is what `hr status` renders.

Each entry in a provider's `keys` array also reports `requests` — how many times that specific
**provider key** (last 6 chars only) has been used since the proxy started. The built-in web
dashboard shows this as a tooltip on each key's status dot.

The `rotation` block reports key selection mode (`{"rotation": {"mode": "key-affinity"}}`);
the `limits` block reports per-key budgets and live usage; `hr status` shows limits in the footer.
See [configuration.md](configuration.md) for details.

## Capacity / pacing (`/v1/capacity`)

`GET /v1/capacity` (access key required) returns a **compact pool capacity signal** for clients
that should slow down or pause work when free-tier headroom is thin — for example Hermes Agent
cron jobs that stretch their interval or skip a tick.

The score blends authoritative **model-scope** rate-limit headroom with provider health
(circuit breaker + recent health bucket). The headroom input is **comparable**: remaining
tokens on the group's binding window, divided by the global max cap for that window across
model-scope TBF groups (not the raw fill fraction of the group's own cap). Response fields:

- `capacity` — float in `[0, 1]`
- `advice` — `fast` | `normal` | `slow` | `skip`
- `interval_multiplier` — multiply your base cron interval by this (e.g. `2.0` → wait twice as long)
- `skip` — when `true`, do not start work this tick
- `reasons` / `components` — diagnostics for humans

```bash
curl -H "Authorization: Bearer sk-router-1" http://localhost:8319/v1/capacity
hr pace            # one-liner: advice=… skip=… mult=… capacity=…
hr pace --json     # raw JSON for scripts
```

### Hermes Agent cron contract

1. At tick start: `hr pace --json` (or curl `/v1/capacity`).
2. If `skip` is true → exit without starting work.
3. Otherwise run the job; schedule the next run as `base_interval × interval_multiplier`.
4. If the endpoint is unreachable → fail-open: treat as `slow` / multiplier `2.0` / `skip=false`
   (`hr pace` prints those defaults and exits non-zero).

## Request log (`/v1/logs`)

`GET /v1/logs` (access key required) returns the most recent requests from an **in-memory ring
buffer** — the data source behind the web dashboard's live log. It never writes to disk: the
last `REQUEST_LOG_SIZE` entries (default **500**) are kept in RAM and the oldest fall off as new
ones arrive (~250 KB at the default size). Set `REQUEST_LOG_SIZE=0` to disable it entirely.

Each entry records: timestamp, endpoint (`chat`/`messages`/`embeddings`), caller (key tail),
streaming flag, complexity score (1–5), estimated tokens, chosen provider + model, latency,
`failed` / `skipped` counts, `cascades` (`failed + skipped`; older builds counted only failed
forwards), `cascade` (ordered steps with `provider`, `model`, `outcome`, `reason`), status
(`success`/`error`/`cache_hit`), and prompt/completion token counts.
Request and response **content is never stored** — only metadata. The dashboard Fail/Skip cell
opens the full cascade path when a trail is present.

Query parameters (all optional): `limit` (default 100), `provider`, `status`
(`success`/`error`/`cache_hit`), and `endpoint` (`chat`/`messages`/`embeddings`).

```bash
curl -H "Authorization: Bearer sk-router-1" \
  "http://localhost:8319/v1/logs?limit=20&status=error"
```
