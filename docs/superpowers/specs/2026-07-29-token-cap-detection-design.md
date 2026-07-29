# Adaptive Per-Model Token Cap Detection — Design Spec

**Date:** 2026-07-29  
**Project:** hermes-router  
**Status:** Approved, awaiting implementation plan

---

## 1. Goal

Some providers (notably Cerebras and Groq free tiers) impose **artificial input and/or
output token ceilings** below the model's advertised context window. Today hermes-router
handles this only via static env knobs and a few hardcoded defaults:

- `{PROVIDER}_SKIP_TOKENS_OVER` — skip the provider when estimated prompt tokens exceed a ceiling
- `{PROVIDER}_MAX_OUTPUT_TOKENS` — clamp `max_tokens` / `max_completion_tokens` before send

Those defaults drift as free tiers change, and they are **provider-wide**, not per model.

**Goal:** detect effective input/output token caps for **every `(provider, model)` in use**,
preferring `/models` metadata when available and falling back to adaptive learning from
real traffic (same spirit as TBF rate learning, but a separate subsystem). Use learned caps
to auto-skip oversized requests and clamp output length.

---

## 2. Scope

### In scope

- New `TokenCapTracker` module (sibling of `AdaptiveRateLimiter`, not folded into it)
- Per-`(provider, model)` `max_input` / `max_output` state with persistence
- Seed from `/models` metadata when fields exist
- Passive learning: cut on classified token-limit failures; gentle raise on near-cap successes
- Wire into routing skip + `forward()` clamp (augment existing env/defaults)
- Env outer fence: discovery may only **tighten** within `{PROVIDER}_SKIP_TOKENS_OVER` /
  `{PROVIDER}_MAX_OUTPUT_TOKENS` when those are set (`0` = no fence)
- Escape hatch `TOKEN_CAPS=0` to disable learning and effective adaptive caps
- Expose caps in `/v1/status`; unit + mocked integration tests
- Docs / `.env.example` for new knobs

### Out of scope

- Active binary-search probing that burns quota at startup
- Folding token caps into TBF request/token buckets
- Live integration tests against Cerebras/Groq
- Dashboard UI beyond status fields (follow-up if trivial)
- Changing TPM/RPM rate-limit behavior

---

## 3. Approach

**Dedicated `TokenCapTracker`** — approach 1 from brainstorming.

Rate limits answer “how fast may we send?” Token caps answer “how large may one request be?”
Keeping them separate avoids muddying TBF groups (which are key-scoped) with product limits
(which are model-scoped).

---

## 4. Architecture

```
/models metadata ──seed──► TokenCapTracker ◄──cut/raise── traffic (classified)
                              │
                              ▼
                    effective_input_cap / effective_output_cap
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
     skip candidate if                  clamp max_tokens
     est_tokens ≥ input cap             before upstream send
```

- **Scope:** `(provider, model)` — not per-key.
- **Persistence:** `token_caps_state.json` (path overridable via `TOKEN_CAPS_STATE_FILE`).
- **Not inside TBF:** no shared buckets or group keys with the rate limiter.

---

## 5. Components

### 5.1 `TokenCapTracker` (`token_caps.py`)

In-memory map keyed by `(provider, model)`:

| Field | Meaning |
|---|---|
| `max_input` | Effective input ceiling (tokens), or unset |
| `max_output` | Effective output ceiling (tokens), or unset |
| `source` | `metadata` \| `learned` \| `mixed` |
| `updated_at` | Unix timestamp of last change |

Public API (names indicative):

- `seed_from_metadata(provider, model, meta: dict)`
- `effective_input_cap(provider, model, env_bound: int) -> int | None`
- `effective_output_cap(provider, model, env_bound: int) -> int | None`
- `on_token_limit_failure(provider, model, kind, observed_tokens)`
- `on_success_near_cap(provider, model, kind, used_tokens)`
- load / save state (fail-soft)

Thread-safe (lock around map mutations), same style as the rate limiter.

### 5.2 Metadata extraction

Extend `/models` parsing so each catalog item can contribute caps. Recognize common fields
when present (non-exhaustive; first hit wins per dimension):

- Input-ish: `context_length`, `max_model_len`, `max_input_tokens`, nested provider variants
- Output-ish: `max_completion_tokens`, `max_output_tokens`, `max_tokens`

Missing fields leave that dimension unknown. Do not invent defaults from model name heuristics
in v1.

### 5.3 Error classifier

Token-limit learning triggers only when:

- HTTP **413**, or
- HTTP **400** whose body matches known phrases (case-insensitive), e.g. context length,
  maximum context, too many tokens, `max_tokens` / `max_completion_tokens` exceeded,
  prompt too long

Uncertain 400s → **do not learn** (still skip/cascade as today). Prefer explicit input vs
output from the message; if ambiguous: large `est_tokens` → input; oversized requested
`max_tokens` → output.

### 5.4 Router wiring

- Candidate skip uses `effective_input_cap(provider, model, env_bound)` only — the env
  bound is the provider-wide fence; the tracker supplies per-model tightening. Skip the
  **candidate** (`provider`/`model`), not the entire provider, when
  `est_tokens >= effective_input_cap` (so a sibling model with a higher cap can still run).
- `forward()` clamps via `effective_output_cap(provider, model, env_bound)` the same way.
- Existing hardcoded defaults (`groq`, `sambanova`, `github_models`, `cohere`, …) remain as
  **initial provider-wide env-bound defaults** when the corresponding env var is unset.
- On classified failure: update tracker, then existing cascade rules (413 → skip provider for
  this request; token-400 → skip model).
- On success near cap: gentle raise under the env fence.

### 5.5 Observability

- `/v1/status`: per-model `token_caps` (`max_input`, `max_output`, `source`)
- Log lines prefixed `[token-cap]` for cuts, raises, and seeds

---

## 6. Effective cap formula

```
effective = min(
  env_bound if env_bound > 0 else +∞,
  tracker_value if set else +∞,
)
# both ∞ → no skip / no clamp (current behavior)
```

**Precedence rule (approved):** env is a provider-wide **outer fence**. Per-model metadata
and learning may only **tighten** further inside that fence. They never loosen past the env
bound when the bound is set.

---

## 7. Learning rules

### Cuts (failure)

- `new_cap = max(MIN_CAP, int(observed * CUT_FACTOR))` with `CUT_FACTOR = 0.9`,
  `MIN_CAP = 256`
- Never raise on failure; never exceed env outer bound
- Dimension: input uses failed request’s estimated prompt tokens; output uses requested
  `max_tokens` / `max_completion_tokens` (or usage if clearer)

### Raises (success)

- Only when used tokens ≥ 85% of current cap (near-cap band)
- Multiplicative nudge `1.05`, still under env fence
- No raise when far below the cap
- Fresh authoritative metadata is respected until traffic **fails** against it (failure may
  then cut below the seeded value)

### Escape hatch

- `TOKEN_CAPS=0` disables adaptive learning and ignores tracker values in
  `effective_*_cap`; static env/defaults behave exactly as today

### Persistence safety

- Corrupt / missing state file → empty tracker + warning; router continues
- Persist after meaningful updates (debounced or immediate is an implementation detail;
  must survive restart)
- Add `token_caps_state.json` to `.gitignore` (runtime state, like `router_state.json`)

---

## 8. Configuration

| Item | Default | Notes |
|---|---|---|
| `TOKEN_CAPS` | `1` (on) | `0` disables adaptive caps |
| `TOKEN_CAPS_STATE_FILE` | `./token_caps_state.json` | Persistence path |
| `{PROVIDER}_SKIP_TOKENS_OVER` | existing defaults / `0` | Outer input fence |
| `{PROVIDER}_MAX_OUTPUT_TOKENS` | existing defaults / `0` | Outer output fence |

v1 hardcodes learning constants (`CUT_FACTOR=0.9`, raise `1.05`, near-cap `0.85`,
`MIN_CAP=256`); env knobs for these are optional follow-up.

---

## 9. Data flow (summary)

1. **Startup:** load state → build providers (env bounds) → seed from `/models` when discovery runs  
2. **Request:** estimate tokens → skip if over effective input → clamp output → send  
3. **Response:** classify → cut / near-cap raise / no-op → persist when changed  

---

## 10. Testing

### Unit (`tests/test_token_caps.py`)

- Effective cap combinations: env-only, metadata-only, learned-only, `min(env, learned)`, unset
- Failure cut; near-cap raise; far-below success does not raise
- Env fence blocks raises above bound; `TOKEN_CAPS=0` path
- Classifier positives / negatives; input vs output tagging
- Persistence round-trip; corrupt file fail-soft

### Integration (mocked HTTP)

- Learned input cap skips upstream call for oversized estimate
- Output cap clamps body before send
- Mocked 413 / token-400 updates state; unrelated 400 does not
- `/models` fixture with `context_length` seeds tracker

### Out of scope for v1 tests

- Live provider calls

---

## 11. Migration / compatibility

- Existing env vars and hardcoded skip/max-output defaults keep working as outer fences /
  initial bounds
- No change to TBF or circuit-breaker semantics
- New state file is additive; deleting it only forgets learned caps

---

## 12. Success criteria

- Models with `/models` context metadata get seeded input caps without waiting for failures
- Cerebras/Groq-style token-limit errors teach a tighter per-model input (and/or output) cap
  and subsequent oversized requests are skipped or clamped without a wasted guaranteed-fail
  round-trip
- Env fences still constrain behavior when operators set them
- Unrelated 400s do not poison caps
