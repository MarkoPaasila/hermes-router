# Hermes Router

A free-tier AI **proxy** that sits between an app and a pool of LLM providers, selecting where each request goes and falling back when one cannot serve it.

## Language

### Product & surfaces

**Proxy**:
The hermes-router service itself — the front door your app talks to instead of talking to providers directly.
_Avoid_: Gateway; Router (as a common noun for the product — the repo/binary may still be named hermes-router)

**hermes-router**:
The product family name for this proxy (service/binary and related tools).
_Avoid_: Calling the product a Router in prose; Gateway

**CLI**:
The `hr` command-line tool for setup, auth, status, limits, features, and service control.
_Avoid_: Dashboard (for terminal `hr status` output)

**Dashboard**:
The proxy's browser UI at `/dashboard` (and `/`).
_Avoid_: Using dashboard for `hr status`

**Hermes Agent**:
A separate third-party VS Code extension (not hermes-router). Call it **Hermes Agent (third-party)** when contrasting session-id or client behavior.
_Avoid_: Hermes; the extension (ambiguous)

**Client**:
Anything that sends requests to the proxy (an application, SDK, Copilot Chat, curl, or similar).
_Avoid_: Caller; App (as the only general noun — fine in app-specific examples)

### Selection & catalog

**Selection**:
The act of choosing which `(provider, model)` candidate should handle a request (by complexity, general intelligence ranking, cost, and session affinity).
_Avoid_: Routing, smart routing, dispatch (as names for this act)

**Fallback**:
Moving to the next candidate when the current one cannot serve the request — including rate-limits, errors, timeouts, and GI / below-threshold skips. Rate-limits are expected capacity exhaustion, not failure.
_Avoid_: Failover (implies the prior attempt "failed"; rate-limits are not failures)

**Cascade**:
The ordered walk through catalog candidates for a single request — try this one, then the next, then the next. Fallback is *why* you leave a candidate; cascade is *how* the proxy walks the list.
_Avoid_: Using cascade as a synonym for the product, or as the user-facing name for recovery (prefer Fallback)

**Catalog**:
The ordered set of candidates the proxy may try for a request.
_Avoid_: Pool (reserve for credentials/providers as a group, not the scored try-list)

**Candidate**:
One selectable option in the catalog: a `(provider, model)` pair. Keys are credentials used while trying a candidate, not part of the candidate identity.
_Avoid_: Calling a provider alone or a key a candidate

**Session affinity**:
Reusing the same candidate across turns of a conversation when the client sends a session id, until fallback forces the cascade to leave that candidate.
_Avoid_: Session-sticky routing, stickiness (as the name for this behavior)

**Key affinity**:
Preferring the same provider key while trying a candidate until that key cannot serve (error or exhausted), then moving to the next ready key for that candidate.
_Avoid_: Sticky-until-fail, sticky key selection (as user-facing names)

**Session**:
A client-supplied id that scopes session affinity — reuse of a candidate (and its preferred provider key) across turns. The proxy never invents a session id and does not store message history under it.
_Avoid_: Using session for chat transcript, agent memory, or RAG; prefer conversation / message history for those

**Preference**:
A client-supplied selection hint (e.g. `fast` via `hermes-router:fast` or `X-Hermes-Profile`) that biases which candidates are tried first. Not a model.
_Avoid_: Profile, conversation mode (as the name for this hint); Model (for `:fast` and similar)

### Providers & models

**Provider**:
A configured upstream backend the proxy can call (a named integration such as Gemini, Groq, OpenRouter, or `local`). Not a model and not a candidate.
_Avoid_: Using provider when you mean model or candidate

**Model**:
A real upstream model id offered by a provider (e.g. `gemini-2.5-flash`). Together with a provider it forms a candidate.
_Avoid_: Calling the virtual proxy id or a selection preference a model

**Proxy model id**:
The virtual model id clients point their SDK at (default `hermes-router`, configurable via `ROUTER_MODEL_ID`). Not an upstream model.
_Avoid_: Model (for this virtual id)

**local**:
A provider whose models run on the operator's machine via an OpenAI-compatible base URL (Ollama, LM Studio, llama.cpp, etc.). Keyless; still a normal provider in the catalog.
_Avoid_: Treating "local models" as a separate product concept outside Provider/Model; Conversation mode (use Preference)

**Codex**:
The `codex` provider — ChatGPT subscription access via OAuth accounts and Responses API translation. A normal provider in the catalog; its credentials are accounts, not provider API keys.
_Avoid_: Treating Codex as a non-provider product type; Provider key (for Codex OAuth credentials — say account)

**Embedding**:
A numeric vector representing the meaning of text, served at `/v1/embeddings`.
_Avoid_: Treating embeddings as chat catalog selection

**Embedding model**:
The model id configured for a provider's embeddings path (`*_EMBED_MODEL`), distinct from that provider's chat model list.
_Avoid_: Model (when you mean the embeddings-specific id)

**Vision**:
The capability to accept image input on a chat/messages request. When images are present, selection only considers vision-capable candidates.
_Avoid_: Image generation (out of scope); Vision routing (prefer selection + vision filter)

**Specialized model**:
A purpose-specific model id (TTS, STT, image generation, embeddings, moderation, etc.) that model discovery may filter out of the chat catalog. Explicitly configured model lists are not filtered this way.
_Avoid_: Unsuitable model (for this)

**Unsuitable model**:
A candidate temporarily cooled after the upstream rejected it for chat (404 or model-not-found-style 400), so later requests skip it until backoff expires.
_Avoid_: Specialized model; Failure (these rejects are not health failures for the circuit breaker)

**Exclude list**:
Per-provider model ids the operator permanently bars from the catalog (configured or discovered).
_Avoid_: Unsuitable model (temporary); Specialized model (discovery filter only)

### Credentials & limits

**Access key**:
A secret the app (or dashboard user) presents to the proxy to prove it may use the proxy. Configured via `PROXY_API_KEYS`.
_Avoid_: Proxy key, caller key (as user-facing names for this credential)

**Provider key**:
A secret the proxy presents to an upstream provider to use that provider's API.
_Avoid_: API key alone when the access-key / provider-key distinction matters; credential (too vague for users)

**Credential pool**:
The set of provider keys the proxy holds and draws from (where key affinity applies).
_Avoid_: Pool alone; provider pool (when you mean catalog); using pool for the catalog of candidates

**auth.json**:
The proxy's file for provider keys and related auth/limit config (path overridable via `ROUTER_AUTH_FILE`). Primary key store; `.env` keys are a fallback.
_Avoid_: Calling `.env` the credential store; inventing a second product name for this file

**Rate limit**:
An upstream provider's quota on how much you may use them in a time window (e.g. requests or tokens per minute). Hitting one is capacity exhaustion and a reason to fall back — not a failure.
_Avoid_: Using "rate limit" for access-key ceilings or per-model size limits

**Budget**:
An operator-imposed ceiling on an access key (RPM, daily requests/tokens/cost). Enforced by the proxy before any provider is contacted.
_Avoid_: Rate limit (for this concept); calling upstream quotas a budget

**Token cap**:
A per-model ceiling on input or output size (how large a request/reply that model can take), including learned adaptive caps.
_Avoid_: Rate limit, budget (for size ceilings)

**Headroom**:
How much learned rate-limit capacity remains for a provider key / model (used for ranking and display).
_Avoid_: TBF, token-bucket filter (as user-facing names)

### Scores & health

**Complexity**:
How hard a request is, scored 1–5 where **1 = easiest** and **5 = hardest**.
_Avoid_: Difficulty (synonym clutter); inverting the scale in user copy

**General intelligence ranking (GI)**:
How strong a model is for general chat/reasoning, scored **0–100** where higher = stronger. Defaults from the LMSYS (+ optional Artificial Analysis) snapshot (`gi_rankings.json`), with id normalization and optional aliases; dashboard overrides win. Unknown models score **0** until overridden.
_Avoid_: Capability or Rating (as the user-facing name for this score); inverting the scale in user copy

**Failure**:
An unexpected upstream health problem (network error or 5xx) — not a rate-limit, bad request, GI / below-threshold skip, or model-not-found. Failures can cool a provider key and feed the circuit breaker.
_Avoid_: Calling rate-limits or ordinary fallback reasons "failures"

**Circuit breaker**:
A per-provider guard that temporarily pulls a provider out of rotation after repeated failures, then re-probes it.
_Avoid_: Using circuit breaker language for rate-limits or budgets

**Feature probing**:
Startup checks that learn what a configured model can do (e.g. tool calling, reasoning), cached for reuse across restarts.
_Avoid_: Model discovery (for this); Probe alone when discovery could be meant; Capability probing (prefer this name)

**Model discovery**:
An add-on that refreshes which model ids a provider offers (from its `/models` list) and may add them to the catalog.
_Avoid_: Feature probing; Discovery alone when probing could be meant

**Reasoning model**:
A model the proxy treats as using hidden chain-of-thought tokens before the visible answer (detected by probing or override), so it may reserve extra output budget. Still a normal model, not a separate candidate kind.
_Avoid_: Treating "reasoning" as a third axis beside provider/model in the catalog identity

**Guardrails**:
Proxy defenses that reject, skip, or clamp a request so bad or oversize calls don't waste upstream capacity (body-size reject, large-payload skip, output clamp).
_Avoid_: Token cap, Budget, Rate limit (different concepts)

### Cache, features & tools

**Response cache**:
Serving a prior answer for a request so the proxy need not call a provider again. Default behavior is exact-match, in-memory, namespaced by access key.
_Avoid_: Cache alone when exact vs semantic matters

**Exact match**:
A response-cache hit when the request is identical (within the access-key namespace).
_Avoid_: Semantic cache (for identical hits)

**Semantic cache**:
An add-on that allows response-cache hits for similar prompts, not only identical ones.
_Avoid_: Calling this the default response cache

**Core**:
Always-on proxy behavior (auth, credential pool, selection, fallback, circuit breaker, protocol translation, feature probing, guardrails, usage/cost tracking, and related essentials).
_Avoid_: Smart routing, failover (as labels inside core — use Selection and Fallback)

**Add-on**:
Optional behavior toggled via `hr features` (or richer config). Most default off; the response cache is the default-on add-on.
_Avoid_: Assuming every add-on is off by default; Feature alone when core vs add-on matters

**Tool calling**:
Letting the model invoke client-defined tools (functions) instead of only returning text. Selection only considers tool-capable candidates when tools are present.
_Avoid_: Function calling (except as a one-time synonym); Tools routing

**Tool**:
A client-defined function the model may invoke (name, parameters, and result handled by the client).
_Avoid_: Function (as the primary user-facing name)

**Protocol translation**:
Adapting request and response shape between the client's API (OpenAI and/or Anthropic) and what the chosen provider needs (including Codex Responses API).
_Avoid_: Format translation (as a competing primary name)

### Usage & spend

**Usage**:
Raw activity counters (requests, tokens) for providers, keys, or access keys.
_Avoid_: Spend (for raw counters)

**Spend**:
Estimated money attributed to serving traffic, from the built-in price table (free and subscription plans can be $0).
_Avoid_: Cost (as the primary dashboard noun for "how much we've burned"); Usage (for money)

**Cost**:
Unit prices in the price table, or budget ceilings denominated in money (e.g. daily cost limits on an access key).
_Avoid_: Using cost as the main synonym for cumulative spend in the Dashboard
