# Using hermes-router from your app

hermes-router speaks the **OpenAI API**. Point any OpenAI-compatible client at the proxy
and it works unchanged.

`api_key` is any value from `PROXY_API_KEYS` (default `sk-router-1`; set your own in
`.env` — see [configuration.md](configuration.md)).

## OpenAI SDK

Point any OpenAI client at `http://localhost:8319/v1`, model `hermes-router`:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8319/v1", api_key="sk-router-1")
resp = client.chat.completions.create(
    model="hermes-router",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Streaming (`stream=True`) and tool calling (`tools=[...]`) both work.

> **Tip — multiply your free quota:** give a provider several models with a comma-separated
> `<PROVIDER>_MODEL` (e.g. `GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash`). Since
> rate limits are per-model, the proxy falls back across a provider's models before moving
> on — capacity scales with keys × **models** × providers. See
> [Configuration](/configuration/#multiple-models-per-provider).

## Listing and pinning a model

`GET /v1/models` returns the virtual proxy model id (`hermes-router`), every
model currently in the live chat catalog, and may include `hermes-router:fast`
when a local provider is configured. Send one of those catalog ids as `model`
on `/v1/chat/completions` to **pin** selection to that logical model:
the proxy only tries providers that offer it (matched after normalizing org
prefixes and tags), then falls back across those candidates. If none can serve,
the request errors — it does **not** substitute a different model.

Before this behavior was introduced, the proxy largely ignored arbitrary
`model` values and auto-selected a model. Now ids other than `hermes-router`,
`auto`, and `:fast` variants are pinned; unknown ids return a
`400 invalid_request_error` instead of falling back to automatic selection.

Use `model: "hermes-router"` (default) when you want full-catalog auto selection.

## Tool use

Pass OpenAI-format `tools` on `/v1/chat/completions`. When a request carries tools, the proxy
**routes only to tool-capable candidates** (detected at startup). Override detection per provider
with `<PROVIDER>_SUPPORTS_TOOLS=1` / `=0` (see [configuration.md](configuration.md)).

## Embeddings

The proxy also speaks the OpenAI **embeddings** API, backed by free providers (Gemini,
Mistral, Cohere):

```python
resp = client.embeddings.create(model="hermes-router", input="hello world")
print(len(resp.data[0].embedding))   # e.g. 3072 from Gemini
```

Unlike chat, embeddings use a **stable provider** (not round-robin): vectors from
different providers have different dimensions and can't be mixed in one store, so the
router keeps hitting the same provider and only falls back if it goes down. For a strict
single-dimension guarantee, disable the others' embed models (e.g. `MISTRAL_EMBED_MODEL=`
and `COHERE_EMBED_MODEL=` empty in `.env`).

## Reasoning models

Some models (e.g. gpt-oss, Nemotron, GLM-4.5) spend output tokens on hidden
chain-of-thought before answering. The proxy detects these at startup and reserves extra
output budget for them, so a small `max_tokens` never yields an empty reply. Tune with
`REASONING_TOKEN_RESERVE` (see [configuration.md](configuration.md)).
