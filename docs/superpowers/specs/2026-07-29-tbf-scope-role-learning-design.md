# TBF Scope-Role Learning — Design Spec

**Date:** 2026-07-29  
**Project:** hermes-router  
**Status:** Approved, awaiting implementation plan

---

## 1. Goal

`AdaptiveRateLimiter` already tracks two AND-gated scopes per API key:

- **Provider-wide** — `provider:{name}|key:{suffix}`
- **Model** — `provider:{name}|key:{suffix}|model:{model}`

Today, header updates, hard 429 cuts, and success nudges are applied to **both**
scopes. Caps and remaining tend to re-mirror, so the Rate limits dashboard cannot
show which scope is actually binding.

**Goal:** give each scope an explicit learning **role** so numbers diverge in
normal traffic, and label that role in the API/dashboard for ops clarity.

Primary success criterion: after mixed traffic (successes + a model 429),
provider-wide and model caps/remaining differ enough that an operator can tell
which row is the binding constraint.

---

## 2. Scope

### In scope

- Role semantics for the two scopes (authoritative vs estimate)
- Asymmetric learning in `AdaptiveRateLimiter`:
  - headers → model only
  - 429 → hard cut on model, soft cut on provider-wide; Retry-After remains model-only
  - success nudge → faster recovery on provider-wide
- New env knobs for soft cut and provider-wide nudge rates
- `role` field on `list_groups` / `/v1/rate-limits`
- Small dashboard labeling (scope column / detail modal / panel blurb)
- Unit tests in `tests/test_rate_limiter.py`
- Docs: `documentation/configuration.md`, website mirror if kept in sync, `.env.example`
- Brief architecture note that PW is a shared-ceiling estimate

### Out of scope

- Parsing 429 bodies to classify “account-wide vs model” limits
- Changing AND-gate consume semantics or credential rotation modes
- Provider-specific header parsers
- Historical charts or divergence metrics
- Changing `/v1/status` snapshot shape beyond what `list_groups` already covers
  (optional `role` on snapshot is not required)

---

## 3. Roles

| Scope | `scope` | `role` | Meaning |
|---|---|---|---|
| Provider-wide | `provider_wide` | `estimate` | Shared-ceiling estimate across models on the key |
| Model | `model` | `authoritative` | Per-model limit learned from upstream |

Unchanged behavior:

- `check_and_consume` still requires both groups; model failure rolls back
  provider-wide debit
- Persistence, flush thread, inactive sweep
- Retry-After / `blocked_until` only on the model group
  (existing sibling-failover behavior)

---

## 4. Learning rules

### 4.1 Headers (`update_from_headers`)

Apply `x-ratelimit-*` only to the **model** group.

Provider-wide inventory and caps are **not** overwritten by response headers.
They move only via consume / restore / soft 429 / faster nudge.

### 4.2 429 (`on_429`)

| Step | Model | Provider-wide |
|---|---|---|
| Apply rate-limit headers from the 429 response | Yes | No |
| Cut buckets not set by headers | Hard (existing) | Soft (new) |
| Zero tokens on cut buckets | Yes | Yes |
| Apply `Retry-After` → `blocked_until` | Yes | No |

**Hard cut (model, existing):**

- If `_period_consumed >= 3`: `cap = max(1, observed_rate * RATE_LEARN_CUT_FACTOR)`
- Else: `cap = max(1, cap * 0.5)`

**Soft cut (provider-wide, new):**

- If `_period_consumed >= 3`: `cap = max(1, observed_rate * RATE_LEARN_CUT_FACTOR_PROVIDER)`
- Else: `cap = max(1, cap * RATE_LEARN_SOFT_CUT_FACTOR)`

Defaults:

| Env | Default | Purpose |
|---|---|---|
| `RATE_LEARN_CUT_FACTOR_PROVIDER` | `0.95` | Soft multiplier vs observed rate on PW |
| `RATE_LEARN_SOFT_CUT_FACTOR` | `0.9` | Soft multiplier vs current cap when little period history |

Model knobs `RATE_LEARN_CUT_FACTOR` (default `0.8`) and the `0.5` low-history
halving stay as today.

Implementation note: prefer a `soft: bool` (or dedicated helper) on
`BucketGroup.on_429` / `TokenBucket.on_429` rather than duplicating cut math at
the limiter layer. Header apply for the provider-wide path must be skipped even
when the 429 response includes `x-ratelimit-*`.

### 4.3 Success nudges (`on_success`)

Both scopes still nudge on success for that `(provider, key, model)` request.
Provider-wide uses faster knobs:

| Env | Default | Applies to |
|---|---|---|
| `RATE_LEARN_SUCCESS_STREAK` | `20` | Model (unchanged) |
| `RATE_LEARN_NUDGE_PCT` | `5` | Model (unchanged) |
| `RATE_LEARN_SUCCESS_STREAK_PROVIDER` | `10` | Provider-wide |
| `RATE_LEARN_NUDGE_PCT_PROVIDER` | `8` | Provider-wide |

Because every model’s successes also call provider-wide `on_success`, aggregate
traffic recovers the shared estimate faster than any single model bucket.

`TokenBucket.on_success` (or the group wrapper) must accept the streak/nudge
parameters for the scope being updated rather than always reading the global
model defaults.

---

## 5. API & dashboard

### 5.1 `list_groups`

Each group dict gains:

```json
"role": "authoritative" | "estimate"
```

Derived from whether the group key contains `|model:` (same rule as today’s
`scope`).

`/v1/rate-limits` returns this field unchanged for the dashboard.

### 5.2 Dashboard

Minimal labeling only (no layout redesign):

- Scope column or detail meta shows role cue (e.g. `estimate` vs
  `authoritative`, or “shared-ceiling estimate — no header sync” in the detail
  modal for provider-wide)
- Panel blurb notes that provider-wide is an estimate that recovers faster and
  does not sync from upstream headers

### 5.3 Clear group

Unchanged: clearing a group drops that scope’s learned state; it re-seeds from
defaults on next use.

---

## 6. Data flow (after change)

```text
request → check_and_consume(pw AND model)
       → upstream response
           ├─ 2xx: restore surplus; update_from_headers(model only);
           │       on_success(pw fast nudge, model normal nudge)
           └─ 429: on_429
                    ├─ model: headers + hard cut + Retry-After
                    └─ pw:    soft cut only (no headers, no hold)
```

Headroom for routing remains `min(pw.headroom, model.headroom)`.

---

## 7. Testing

Add/extend unit tests in `tests/test_rate_limiter.py`:

1. **Headers model-only** — after `update_from_headers`, model caps/remaining
   match headers; provider-wide unchanged.
2. **Asymmetric 429** — model hard-cut; provider-wide soft-cut; provider-wide
   `blocked_until` unset; model hold still works; sibling model still usable
   (existing sibling test remains valid).
3. **Faster PW nudge** — with provider streak/nudge defaults, provider-wide cap
   rises before model after the same number of successes (or after fewer
   successes than the model streak).
4. **`role` field** — `list_groups` reports `estimate` for provider-wide and
   `authoritative` for model.
5. **Regression** — consume rollback, inactive sweep, load clearing legacy
   provider-wide `blocked_until` still pass.

No new router integration test required unless wiring accidentally drifts;
learning lives entirely in `rate_limiter.py`.

---

## 8. Docs

- Document roles + new env vars under Adaptive upstream rate limiter in
  `documentation/configuration.md` (and website content mirror if applicable).
- Comment the new knobs in `.env.example` next to existing `RATE_LEARN_*`.
- Architecture docs: one sentence that provider-wide is a shared-ceiling
  estimate (no header sync; softer cuts; faster recovery), while model groups
  remain authoritative.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Provider-wide estimate drifts above true account ceiling → extra 429s | Soft cut still lowers PW on 429; AND-gate still blocks when model is empty; defaults stay conservative |
| Soft cut too soft → PW rarely binds | Tunable via `RATE_LEARN_CUT_FACTOR_PROVIDER` / `RATE_LEARN_SOFT_CUT_FACTOR` |
| Fast nudge too aggressive | Tunable via provider streak/nudge envs; model path unchanged |
| Ops confuse estimate with upstream truth | Explicit `role` + dashboard copy |

---

## 10. Acceptance

- [ ] Headers never mutate provider-wide buckets
- [ ] Model 429 hard-cuts; provider-wide soft-cuts; Retry-After model-only
- [ ] Provider-wide nudges with shorter streak / larger pct than model
- [ ] `/v1/rate-limits` groups include `role`
- [ ] Dashboard shows role cue for provider-wide vs model
- [ ] Unit tests above green; existing rate-limiter regressions green
- [ ] Config / architecture / `.env.example` updated
