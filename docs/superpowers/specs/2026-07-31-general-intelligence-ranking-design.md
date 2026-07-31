# General intelligence ranking

**Date:** 2026-07-31  
**Status:** approved

## Goal

Replace user-facing **Capability** (integer 1–5 model strength) with a continuous
**general intelligence ranking (GI)** on a 0–100 scale. Defaults come from an
in-repo snapshot combining LMSYS Chatbot Arena and Artificial Analysis
(median of min–max-normalized scores). Operators can set or clear per-model
overrides from the dashboard. Selection keeps “cheapest candidate that clears a
complexity-mapped minimum GI.”

## Terminology

| Term | Meaning |
|------|---------|
| **General intelligence ranking (GI)** | Continuous model strength, 0–100, higher = stronger. Replaces user-facing Capability. |
| **Complexity** | Request difficulty 1–5 (unchanged; 1 = easiest, 5 = hardest). |
| **Feature probing** | Startup checks for tools / vision / reasoning. Not called “capability” in user copy. |
| **GI snapshot** | Checked-in `gi_rankings.json` of default scores. |
| **GI override** | Operator-set score for a `(provider, model)`. Wins over snapshot. Clearable. |

**Avoid:** Capability (for this score); Rating (user-facing); inverting higher = stronger.

**Wire / status:** primary field `gi`. Status also exposes `gi_source`
(`override` | `snapshot` | `default`). Legacy probe-state `rating` integers are
not mapped into GI. New writes use `gi` / feature fields only for strength.

## Score scale & snapshot

- Canonical scale: float **0–100**.
- Snapshot build: for each model id, take available source scores (LMSYS Arena,
  Artificial Analysis), min–max normalize each source into 0–100 independently,
  then take the **median**. If only one source has the model, use that
  normalized value.
- File: `gi_rankings.json` at repo root (path overridable via `GI_RANKINGS_FILE`),
  shape:

```json
{
  "version": 1,
  "generated_at": "ISO-8601",
  "sources": ["lmsys", "artificial_analysis"],
  "models": {
    "gemini-2.5-pro": {
      "gi": 88.5,
      "sources": {"lmsys": 90.0, "artificial_analysis": 87.0}
    }
  }
}
```

- Lookup: longest-substring match on lowercased model id (aliases may be added
  in the refresh script / snapshot keys). Provider-agnostic; per-provider
  differences use overrides.
- Refresh: maintainer script under `scripts/` only. No live leaderboard fetch
  inside the proxy process.

## Resolution

For candidate `(provider, model)`:

1. Dashboard override for that pair, if set
2. Else snapshot match
3. Else **bottom of pack** = `0`

Missing or unreadable snapshot → warn at load; all models resolve as `0` unless
overridden. Corrupt entries are skipped with a log line.

## Complexity → minimum GI

| Complexity | Min GI (defaults) |
|------------|-------------------|
| 1 | 0 |
| 2 | 20 |
| 3 | 40 |
| 4 | 60 |
| 5 | 80 |

Constants in code; optionally overridable later via env. Design is five
thresholds on 0–100, not the old integer equality `rating >= complexity`.

## Selection

In `_get_smart_ordered`:

- Tier 0: `gi >= min_gi(complexity)` — sort by price ascending, then overshoot
  (`gi - min_gi`) ascending, then existing health / rate / availability /
  list_index terms.
- Tier 1: below threshold — last resort; closest GI first.
- Remove `MODEL_QUALITY_RANKS` (and quality term in the sort key).
- Feature filters (tools / vision) unchanged.

## Overrides & dashboard

- Persist in `gi_overrides.json` (env `GI_OVERRIDES_FILE`), separate from
  `router_state.json` probe TTL.
- Key: `"provider|model"` → `{ "gi": number, "updated_at": "..." }`.
- Auth-gated API:
  - `PUT /v1/config/gi-override` body `{ provider, model, gi }` — set (reject
    outside 0–100 with 4xx)
  - `DELETE /v1/config/gi-override` body `{ provider, model }` — clear override
- Models modal: show effective GI + source badge; set and clear controls.
- Apply live in memory (no restart).
- UI: replace 5-pip Capability bars with numeric GI (optional 0–100 bar).
- `/v1/status` `model_caps` entries include `gi`, `gi_source`, plus existing
  `supports_tools` / `reasoning`.

## Edge cases

- Do **not** convert old 1–5 `rating` values into GI.
- LMSYS/AA id mismatch → score `0` until alias or override.
- Cascade / docs language: “too weak for complexity” → GI / below-threshold;
  feature skips stay feature-language.
- Capability probing → **feature probing** in user-facing docs.

## Testing

- Resolution order: override → snapshot → `0`
- Threshold gating and cheapest-eligible ordering
- Override set / clear persistence and live effect
- API validation (out of range; delete)
- Median-of-normalized helper used by the refresh script
- Status / caps expose `gi` (not user-facing Capability for strength)

## Out of scope

- Live periodic fetch of leaderboards from the running proxy
- Soft scoring without a hard min-GI threshold
- Changing the complexity classifier itself
