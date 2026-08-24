# GI refresh (Arena 2026-08-24) + synonym graph

**Date:** 2026-08-24  
**Status:** approved  
**Extends:** [2026-08-10-gi-synonym-matching-design.md](2026-08-10-gi-synonym-matching-design.md),  
[2026-08-01-gi-coverage-design.md](2026-08-01-gi-coverage-design.md)

## Goal

1. **Ship** the planned maintainer-only synonym graph (`scripts/gi_synonyms.py`)
   and wire it into `scripts/refresh_gi_rankings.py`.
2. **Refresh** checked-in GI sources and `gi_rankings.json` from Arena
   **2026-08-24**, a fresh Artificial Analysis export (`AA_API_KEY`), and
   cached OpenRouter + LiteLLM catalogs.

Proxy / `gi_ranking.py` public matching stays unchanged. Uncommitted
specialized-model filter work in `router.py` is **out of scope**.

## Success criteria

- `gi_synonyms.py` + refresh CLI flags match the Aug 10 synonym design
  (`--offline`, `--openrouter`, `--litellm`, synonym step before `--llm`).
- Unit/integration tests pass with offline fixtures (no network in CI).
- Source caches under `data/gi_sources/` updated (LMSYS, AA, OpenRouter,
  LiteLLM, `catalog.json`, pointer).
- `gi_rankings.json` rebuilt with overlay semantics (below); catalog coverage
  ≥ 80% (aim ~100% via overlay + seed + aliases).
- Snapshot `note` / `generated_at` / `sources` accurately describe this run.

## Decisions

| Topic | Choice |
|-------|--------|
| Synonym implementation | Implement Aug 10 design as-is (no proxy changes) |
| LMSYS boards | Arena dump `2026-08-24` text + code |
| LMSYS merge | **Overlay**: new Elo updates matching ids; retain prior LMSYS/seed scores for ids missing from today’s boards |
| AA | Live Data API with `AA_API_KEY`; slug + `artificial_analysis_intelligence_index` → `aa.json` |
| Catalog | Union of current `catalog.json` + OpenRouter chat model ids (normalized) |
| OpenRouter / LiteLLM | Fetch live, write caches; refresh runs `--offline` against those files for reproducibility |
| `--llm` | Optional last resort only if `GI_ALIAS_LLM_*` set and coverage still needs help |
| Commits | (1) synonym feature + tests + docs; (2) data refresh snapshot |
| Out of scope | Default `*_MODEL` env lists; specialized-filter WIP; proxy matching changes |

## Architecture

```
Arena 2026-08-24 ──► lmsys.json ──┐
AA API (AA_API_KEY) ──► aa.json ──┤
prior gi_rankings (overlay/seed) ─┼──► refresh_gi_rankings.py ──► gi_rankings.json
catalog ∪ OpenRouter ids ─────────┤         ▲
OpenRouter + LiteLLM caches ──────┴── gi_synonyms (aliases)
                                    optional --llm
```

## Components

### Fetch & convert (maintainer session)

1. Arena `latest.json` → confirm path `2026-08-24`; save text/code boards and
   convert to `lmsys.json` (`[{id, score}, ...]`). Per id: **mean** of available
   text/code Elo (one board alone → that Elo). Document in snapshot `note`.
2. AA: `GET` models with `AA_API_KEY`; write `aa_raw.json` + shaped `aa.json`.
3. OpenRouter `GET https://openrouter.ai/api/v1/models` →
   `openrouter_models.json`.
4. LiteLLM prices raw GitHub JSON → `litellm_prices.json`.
5. Rebuild `catalog.json` = sorted unique union of prior catalog + OpenRouter
   chat-capable ids (exclude clear non-chat if cheap metadata allows; otherwise
   keep broad union and rely on GI specialty guards at resolve time).

### Overlay into snapshot inputs

LMSYS and seed must not shrink vs the prior checked-in snapshot:

1. **LMSYS Elo file:** `new_board_elo` overwrites on id conflict; ids only in
   prior `data/gi_sources/lmsys.json` are **retained** unchanged.
2. **AA:** fresh export only (no retain of AA ids absent from the new export).
3. **Seed post-pass:** after `build_models_from_sources`, for each prior
   `gi_rankings.json` model whose sources were **only** `seed` (or that remains
   unmatched with no lmsys/aa), copy that prior `gi` into the new snapshot as
   `sources: {seed: <gi>}` so catalog coverage stays complete.
4. Snapshot `sources` list and `note` must mention seed retention when used.

Prefer a named helper in `refresh_gi_rankings.py` (e.g. `apply_seed_overlay`)
over a disposable one-off.

### Synonym graph

Per [2026-08-10-gi-synonym-matching-design.md](2026-08-10-gi-synonym-matching-design.md):

- New `scripts/gi_synonyms.py`
- Wire into `run_refresh` between deterministic match and optional `--llm`
- `data/gi_sources/llm_plugin_aliases.json` (empty `{}` or small seed OK)
- Update `data/gi_sources/README.md` for fetch URLs and `--offline`

### Refresh CLI

```bash
python scripts/refresh_gi_rankings.py \
  --lmsys data/gi_sources/lmsys.json \
  --aa data/gi_sources/aa.json \
  --catalog data/gi_sources/catalog.json \
  --offline \
  --openrouter data/gi_sources/openrouter_models.json \
  --litellm data/gi_sources/litellm_prices.json \
  --out gi_rankings.json
```

Live OpenRouter/LiteLLM fetch remains the default when not `--offline`.

## Error handling

| Failure | Behavior |
|---------|----------|
| Missing `AA_API_KEY` / AA HTTP error | Abort AA fetch with clear message |
| OpenRouter / LiteLLM fetch fail | Abort unless `--offline` or explicit path provided |
| Offline missing required cache file | `SystemExit` with path |
| Catalog coverage &lt; 80% | Exit code 1 (snapshot may still be written; do not commit until fixed or floor waived intentionally) |
| Synonym proposals | Never invent snapshot keys |

## Testing

- `tests/test_gi_synonyms.py` — graph, edges, modality/sibling guards, offline loaders
- `tests/test_refresh_gi_rankings.py` — synonym-before-llm, offline missing file, coverage; existing tests updated to pass empty OR/LiteLLM payloads / `--offline`

## Commit plan

1. **feat:** synonym module + refresh wiring + tests + README  
2. **chore:** refreshed `data/gi_sources/*` + `gi_rankings.json` (Arena 2026-08-24 + AA)

Do not include specialized-filter WIP files in these commits.
