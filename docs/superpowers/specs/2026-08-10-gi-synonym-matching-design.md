# GI synonym matching from catalog ecosystems

**Date:** 2026-08-10  
**Status:** approved  
**Extends:** [2026-08-01-gi-coverage-design.md](2026-08-01-gi-coverage-design.md)

## Goal

Reduce **unmatched catalog IDs** (GI falling through to `0` / `default`) by
enriching maintainer refresh alias generation with a **synonym graph** built
from OpenRouter, LiteLLM, and explicit `llm-*` plugin alias maps. The proxy and
runtime `gi_ranking` resolution stay unchanged; only `gi_rankings.json`
`aliases` become denser.

## Success criteria

- With `--catalog`, refresh matches more catalog ids than deterministic-only,
  without inventing snapshot keys or GI scores.
- Existing safety holds: no mini/lite/flash sibling collapse via graph alone;
  modality-token rules still apply.
- `--llm` remains available as a **last resort** after synonym-assisted matching.
- Live fetch is the default for OpenRouter + LiteLLM; `--offline` (or explicit
  file paths) supports reproducible runs.
- Proxy / `gi_ranking.py` public matching behavior is unchanged.

## Decisions

| Topic | Choice |
|-------|--------|
| Where matching improves | Maintainer refresh only |
| Approach | Synonym graph → denser `aliases` in snapshot |
| OpenRouter | Live `GET https://openrouter.ai/api/v1/models` (or file) |
| LiteLLM | Live LiteLLM prices JSON (or file); chat-mode keys as spellings |
| Simon / `llm` ecosystem | Explicit alias maps from `llm-*` plugins (`register(..., aliases=...)`), checked in as `data/gi_sources/llm_plugin_aliases.json` |
| LLM proposals | Keep `--llm` after deterministic + synonym steps |
| Proxy | No change |

## Architecture

```
LMSYS + AA ──► models{} (snapshot keys + GI)
                      ▲
catalog.json ──► match ──► aliases{}
                      ▲
        synonym graph (OR + LiteLLM + llm-plugin aliases)
                      ▲
                 leftovers ──► optional --llm
```

Refresh builds a temporary synonym graph, then writes only the existing snapshot
shape (`models`, `aliases`, optional `coverage`). Runtime still resolves:
override → exact/normalized/alias → longest contained key → default `0`.

## Components

### Synonym builder (refresh-only)

New helpers (preferred: `scripts/gi_synonyms.py`, imported by
`refresh_gi_rankings.py`):

1. Load/fetch sources → normalized name nodes
2. Add deterministic edges (below)
3. For each catalog id, walk the connected component; collect every synonym
   that `deterministic_match`es a snapshot key; if several hit, keep the same
   winner `deterministic_match` would pick for that synonym (longest contained
   key). If multiple synonyms hit different keys, prefer the longest matching
   snapshot key; ties keep the lexicographically smaller key (stable).
4. Return `aliases: {normalized_catalog_id → snapshot_key}`

### Edge rules

| Edge | Source |
|------|--------|
| Identity after `normalize_model_id` | all |
| OpenRouter `id` ↔ `canonical_slug`, also linking a date-stripped slug form | OpenRouter |
| OpenRouter `id` ↔ `alias_target.slug` | OpenRouter |
| Shared `hugging_face_id` (both sides non-empty and equal) | OpenRouter (+ LiteLLM if present) |
| LiteLLM key ↔ stripped `provider/` form | LiteLLM chat-mode keys |
| Plugin short alias ↔ registered model id | `llm_plugin_aliases.json` |

**Date-stripped slug:** only remove a trailing calendar suffix matching
`-YYYYMMDD` or `-YYYY-MM-DD` (and optional trailing build tags already handled
by `normalize_model_id`). Do not strip numeric version segments that are part
of the model name (e.g. `gemini-2.5-flash`).

### Refuse / do not merge when

- Modality-token mismatch (reuse `gi_ranking.modality_tokens` /
  `allows_contained_match` spirit)
- Graph would map a `*-mini` / `*-lite` / `*-flash` sibling onto a stronger base
  key **without** an exact, `alias_target`, or shared-HF link
- Proposed target is not in the snapshot `models` key set

### CLI (`scripts/refresh_gi_rankings.py`)

| Flag | Behavior |
|------|----------|
| `--openrouter PATH` | Use file instead of live OpenRouter fetch |
| `--litellm PATH` | Use file instead of live LiteLLM fetch |
| `--llm-aliases PATH` | Plugin alias map (default: `data/gi_sources/llm_plugin_aliases.json`) |
| `--offline` | No network; require OpenRouter + LiteLLM files (explicit paths or defaults under `data/gi_sources/`) |
| `--catalog`, `--llm`, `--coverage-floor` | Unchanged roles |

**Default URLs (live):**

- OpenRouter: `https://openrouter.ai/api/v1/models`
- LiteLLM: `https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json`

**Default offline / cache paths:** `data/gi_sources/openrouter_models.json`,
`data/gi_sources/litellm_prices.json`, `data/gi_sources/llm_plugin_aliases.json`.
Missing plugin-aliases file → warn and treat as empty `{}` (do not fail refresh).
Missing OpenRouter or LiteLLM file under `--offline` → fail.

### Outputs

Same `gi_rankings.json` contract. Optional coverage metadata may record match
path counts (`deterministic` | `synonym` | `llm`) for maintainer diagnostics.
Invalid synonym→snapshot links are skipped (never invent GI keys).

### `llm_plugin_aliases.json`

Checked-in maintainer artifact: list or map of short names ↔ full model ids
harvested from `llm-*` plugin registrations. Updating it is a separate manual
(or later scripted) maintainer step — not required inside the proxy, and not
auto-scraped in CI for this design.

## Data flow

1. Build `models{}` from LMSYS / AA (unchanged)
2. Load catalog ids
3. Build synonym graph from OpenRouter + LiteLLM + plugin aliases
4. Per catalog id: deterministic → synonym-assisted → optional `--llm`
5. Write `aliases{}` + coverage; exit `1` if below floor

## Error handling

| Situation | Behavior |
|-----------|----------|
| Live fetch fails | Clear error unless `--offline` or explicit path provided |
| `--offline` missing a required file | Non-zero exit; name the path |
| Bad JSON / empty source | Warn; continue with remaining sources |
| Empty graph | Fall back to today’s deterministic (+ `--llm`) path |
| Synonym target missing from snapshot | Skip that alias |

## Out of scope

- Runtime proxy matching changes or live leaderboard/catalog fetch in the proxy
- Auto-harvesting `llm` plugins in CI
- Family-size heuristics inventing GI scores
- Changing coverage floor default, complexity→min-GI, or selection policy
- Relying on LiteLLM’s `aliases` field (currently unused in upstream JSON)

## Testing

Unit tests with fixtures (no network):

- Tiny OpenRouter / LiteLLM / plugin JSON → expected synonym aliases
- Sibling safety: `gpt-4o-mini` must not inherit `gpt-4o` via graph alone
- `alias_target` and shared HF id produce aliases
- `--offline` file selection / missing-file failure
- Existing `gi_ranking` resolve tests remain green (proxy surface untouched)

## Docs

- `data/gi_sources/README.md`: new sources, `--offline`, default URLs
- Note that this extends GI coverage matching; ADR-0002 proxy offline rule unchanged
