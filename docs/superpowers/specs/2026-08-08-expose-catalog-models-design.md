# Expose catalog models to clients (pin by model id)

**Date:** 2026-08-08  
**Status:** approved

## Goal

Let clients discover every live chat catalog model via `GET /v1/models` and
**pin** chat requests to a chosen model id through hermes-router — so they do
not need direct provider APIs and **usage stats accumulate in the proxy**.

Auto selection via the virtual proxy model id remains the default path.

## Terminology

| Term | Meaning |
|------|---------|
| **Auto mode** | Client sends `hermes-router`, `hermes-router:fast`, `auto`, or empty `model` → full-catalog selection (unchanged). |
| **Pinned mode** | Client sends any other `model` string → only candidates whose model id **normalizes equal** to the request may be tried. |
| **Normalized model id** | Result of `gi_ranking.normalize_model_id` (lowercase; strip `org/`, `:tag`, trailing `-free`, common quant suffixes). |
| **Live chat catalog** | Model ids currently selectable for chat (configured + discovery, after exclude list / specialized filters). |

**Aligns with:** Catalog / Candidate / Selection / Cascade / Fallback / Proxy
model id (`CONTEXT.md`).

## Context (current behavior)

- `GET /v1/models` advertises only `ROUTER_MODEL` (`hermes-router`) and optionally
  `ROUTER_MODEL:fast` when a local provider is configured.
- `POST /v1/chat/completions` always builds a full ranked candidate list via
  `_ordered_providers`; the client’s `model` field is largely a placeholder
  except for the `:fast` preference.
- Stats, logs, cascade trails, and dashboards already record traffic that
  passes through the proxy. Direct provider calls bypass all of that.

## Decision summary

1. **Advertise** the live chat catalog on `GET /v1/models`, plus virtual router ids.
2. **Pin** when `model` is not auto/virtual: filter the ranked catalog to
   normalized matches; **no fallthrough** to other logical models.
3. **Bare model ids** only — no required `provider/model` client API; multiple
   providers offering the same logical model remain competing candidates.
4. **Normalized matching** for pin (reuse `normalize_model_id`); list entries
   keep **catalog spellings** so clients can copy ids that forward upstream.
5. **Reuse** the existing cascade (ranking, key affinity, session affinity,
   tools/vision, rate limits, unsuitable cooling, stats) on the filtered set.
6. **YAGNI:** no separate pin engine, no canonical alias registry beyond the
   existing normalizer, no embeddings listing in this change.

## Architecture

```
GET /v1/models
  → virtual ids (ROUTER_MODEL, optional :fast)
  → unique strings from live chat catalog

POST /v1/chat/completions
  → auto? ──yes──→ today’s full ranked cascade
         └──no───→ ranked cascade
                   → keep candidates where normalize(model) == normalize(request)
                   → empty? → 400 invalid_request_error
                   → else cascade only that set (503 if exhausted)
```

No new public endpoints. No second selection engine.

## Components

### `GET /v1/models`

- Auth: same access-key gate as today.
- Payload: OpenAI list shape (`object: list`, `data: [{id, object, owned_by}, …]`).
- Every entry uses `owned_by: "hermes-router"` (the proxy advertises and serves
  them; clients must not infer a need for upstream provider keys).
- Include:
  - `ROUTER_MODEL`
  - `ROUTER_MODEL:fast` when local is configured (unchanged rule)
  - every distinct model string from the live chat catalog (provider config /
    discovery lists after exclude / specialized filters)
- Listing uses **catalog spellings** (not only normalized forms). Duplicate
  logical models with different spellings may both appear; pin matching still
  collapses them via normalization.

### Pin filter (selection)

- At the start of `_route_completion`, read the client `model` **before** the
  existing `:fast` → `ROUTER_MODEL` rewrite. That original string is the pin
  key when not auto.
- Auto when original `model` is `""`, `ROUTER_MODEL`, `auto`, or ends with
  `:fast` — then apply today’s `:fast` / prefer_local behavior unchanged.
- Pinned otherwise: after `_ordered_providers(...)`, filter to candidates where
  `normalize_model_id(candidate.model) == normalize_model_id(original_model)`.
- Preserve relative order from the existing sort among remaining candidates.
- Apply existing tools / vision / unsuitable / breaker / provider-scope skips
  on the filtered list only — never widen back to non-matching models.

### Forwarding

- Unchanged: cascade picks concrete `(provider, model)`; `forward()` sends that
  provider’s real model string.
- Response / logs / stats continue to record the upstream provider and model
  that served (not the virtual router id).

## Data flow

1. Client discovers ids via `GET /v1/models`.
2. **Auto:** `model: hermes-router` (or `:fast` / `auto`) → full catalog cascade
   → stats as today.
3. **Pinned:** client sends a catalog or alias id → filter by normalized id →
   cascade only matches → success path records actual upstream candidate.
4. Several providers may match one pin; price / GI / health / rate headroom /
   sticky ordering among them is unchanged.
5. **Session affinity:** if sticky `(provider, model)` still matches the pin,
   keep it front. If sticky’s model does not match the pin, do not prefer it
   for this request. Clearing sticky still follows existing leave-sticky rules
   when an affine candidate cannot serve.

## Error handling

| Case | Behavior |
|------|----------|
| Pinned, zero catalog matches (unknown / no normalized hit) | **400** `invalid_request_error`; message that the model is not in the proxy catalog; hint `GET /v1/models`. No auto fallthrough. |
| Pinned, matches exist but all skipped/fail/exhausted | Same exhaustion shape as today (**503** / existing error body); message should indicate the **pinned** model could not be served. |
| Pinned + tools/vision needed, only incapable matches | Exhaust filtered set → error; do not widen to other models. |
| Access-key provider scope | Applied as today on the filtered set. Zero in-scope catalog matches → treat as unknown (**400**). Matches that all fail → **503**. |
| Auto mode | Unchanged. |

## Testing

- **Unit:** auto vs pin detection; normalized equality (org prefix, `:tag` /
  `-free`, case); empty filter → 400 path; filter preserves relative order.
- **API:** `GET /v1/models` includes `ROUTER_MODEL` and at least one real catalog
  id when providers are configured; pinned chat only attempts matching
  candidates (cascade trail / test double); unknown id → 400; auto still
  cascades across models.
- **Regression:** `:fast` still prefers local on easy turns within auto;
  sticky + matching pin stays first; tools/vision enforce on pin subset without
  widening.

## Out of scope

- Qualified client ids (`provider/model`) as a required API.
- Advertising embeddings or non-chat specialized models on `/v1/models`.
- New alias registry beyond `normalize_model_id`.
- Changing how auto selection ranks the full catalog.
- Dashboard UI for browsing the advertised list (API is enough for this goal).

## Success criteria

- A client can list catalog models from the proxy and complete chat with a
  chosen model id without calling providers directly.
- Pinned traffic never silently substitutes a different logical model.
- Auto (`hermes-router`) behavior and stats paths remain intact.
- Pinned successes appear in existing proxy stats/logs like any other cascade
  success.
