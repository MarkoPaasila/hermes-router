# Selection Features

hermes-router doesn't just forward your request to the first provider it finds — it looks at
*what kind* of request it is and picks a candidate that can actually handle it, then falls back
automatically if that one cannot serve it. This page explains each selection feature in plain
language. For the technical version (scoring formulas, code paths), see the
**[Architecture section of the README](../README.md#architecture)**.

**The short version:** every request gets a **complexity** score, every model gets a
**capability** score, and the proxy sends it to the cheapest model that can still do the job —
skipping anything that can't, and cascading to the next option automatically when needed.

## Chat completion selection

This is the core of the proxy, used by every chat request.

- Your message gets a **complexity score from 1 (easiest) to 5 (hardest)** just from reading it —
  no extra AI call. Words like "implement," "design," or "debug" push it toward "hard"; something
  like "what year was X" or "yes or no" pushes it toward "easy."
- Every configured model has a **capability from 1 (weakest) to 5 (strongest)**, based on
  its name (e.g. `gemini-2.5-pro` scores higher than `gemini-2.5-flash-lite`).
- The proxy builds a **full catalog** of every configured `(provider, model)` **candidate** and
  picks the **cheapest model that's still capable enough** for the request — across the whole
  catalog, not one provider at a time. A trivial question never gets sent to your most powerful
  (and often priciest or slowest) model, and a hard question is never left with a model too weak
  to handle it.
- If the chosen model is rate-limited, down, or errors — the proxy **falls back** along the
  **cascade** (ordered try-list). Your client just gets an answer; it never sees the failed attempt.
- Models that return **404** or a **model-not-found / not-supported** style **400** are cooled as
  **unsuitable** with exponential backoff so later requests skip them (payload-shaped 400s still
  cascade once per request; **429** stays on the rate-limit path). Enable
  `FILTER_SPECIALIZED_MODELS` when using a large auto-discovery limit so non-chat IDs are less
  likely to enter the roster in the first place.

This also works *within* a single provider: if you list several models for one provider (e.g.
`GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.5-pro`), the proxy treats each one
as its own catalog candidate — easy requests land on `flash-lite`, hard ones climb to
`gemini-2.5-pro` — instead of only using the extra models as backups.

### Session affinity

When your client sends a **session id**, the proxy remembers the winning
`(provider, model, key)` for that conversation and reuses it on later turns until **fallback**
forces the cascade to leave that candidate (real upstream 429 / Retry-After, error, capability
skip, etc.). Learned rate-limit **headroom estimates** do not abandon the affine candidate —
the session is allowed to bump into limits so learning can happen. That keeps multi-turn chats on
the same upstream account and model instead of bouncing around the catalog every message.

**Session id resolution** (first non-empty wins; the proxy never invents an id):

1. Header `X-Hermes-Session-Id`
2. Header `X-Chat-ID`
3. Body field `user` (OpenAI-style)
4. Body `metadata.session_id` or `metadata.sessionId`

With **no session id**, every request gets a fresh full-catalog pick.

Within a session, **keys use key affinity**: the same provider key is preferred until
that key errors or is exhausted, then the proxy tries the next ready key for that model.

> **Hermes Agent (third-party) note:** the VS Code Hermes Agent extension does **not** yet
> forward a session id to custom OpenAI-compatible endpoints, so session affinity won't activate
> there until it does. Other clients can set the headers or body fields above explicitly.

## Tool-calling selection

**Tool calling** (or "function calling") is how you let the model *do* something — like look up
the weather — instead of just answering in text. See **[concepts.md](concepts.md)** if that's new to you.

Not every model can do this. When your request includes `tools`, the proxy **only considers
models known to support tool calling** — skipping ones that would just ignore the tool and
answer conversationally instead of calling it. This is detected automatically per model at
startup (see [configuration.md](configuration.md#per-provider-capability-overrides) to override a
result manually with `<PROVIDER>_SUPPORTS_TOOLS`).

**Safety net:** if the proxy can't confirm *any* candidate supports tools (e.g. every provider is
new/unprobed), it doesn't hard-fail — it falls back to trying all of them rather than refusing the
request outright.

## Vision selection

When your request includes an image, the proxy **only considers models known to accept image
input**, for the same reason as tool-calling: sending an image to a text-only model just wastes a
round-trip on a guaranteed rejection.

This works whether your client talks to the proxy in **OpenAI format** (`image_url` content blocks)
including OpenAI `image_url` content blocks — vision-capable candidates are selected automatically
so the image actually reaches the model, regardless of which SDK you're using.

Same safety net as tool-calling: if no known vision-capable candidate exists among your configured
providers, the proxy falls back to trying all of them instead of refusing the request.

## Embeddings selection

**Embeddings** turn text into a list of numbers representing its *meaning*, used for things like
"find the most similar document." See **[concepts.md](concepts.md)** for the plain-language version.

This is a separate, simpler path — only providers configured with an **embedding model**
(currently Gemini, Mistral, Cohere, and OpenAI) are candidates. `POST /v1/embeddings` uses the
same fallback behavior as chat completions: if one embedding provider cannot serve, the next is
tried automatically.

## What isn't selected (yet)

To set expectations clearly: hermes-router does **not** currently handle **image
generation** (there's no DALL-E-style "create an image" endpoint) or **audio/speech**. It only
handles text and vision *input* — reading images you send it, not generating new ones.

## Want the full technical picture?

This page is the plain-language tour. For the exact scoring formula, the request pipeline
diagram, and how each of these interacts with fallback, the circuit breaker, and capability
probing under the hood, see the **[Architecture section of the README](../README.md#architecture)**.

---

**Next:** [Deployment](deployment.md) — run the proxy on your OS (Windows, macOS, Linux).
