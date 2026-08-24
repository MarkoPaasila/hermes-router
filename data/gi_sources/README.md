# GI source exports (maintainer)

Inputs for `scripts/refresh_gi_rankings.py`.

## LMSYS / Arena AI

Fetch a text (and optionally code) leaderboard snapshot, then convert to
`[{ "id", "score" }, ...]`:

```bash
curl -fsSL "https://raw.githubusercontent.com/oolong-tea-2026/arena-ai-leaderboards/main/data/latest.json"
# then data/{date}/text.json and optionally code.json
```

A converted `lmsys.json` may be regenerated locally; it is not required at proxy runtime.

## Artificial Analysis

Export Intelligence Index scores via the [Artificial Analysis Data API](https://artificialanalysis.ai/data-api)
(`AA_API_KEY`), shape as `[{ "id": "<slug>", "score": <intelligence_index> }, ...]`,
then:

```bash
python scripts/refresh_gi_rankings.py \
  --lmsys data/gi_sources/lmsys.json \
  --aa data/gi_sources/aa.json \
  --catalog data/gi_sources/catalog.json \
  --llm \
  --out gi_rankings.json
```

`--llm` needs `GI_ALIAS_LLM_BASE_URL`, `GI_ALIAS_LLM_API_KEY`, and `GI_ALIAS_LLM_MODEL`.
Exit code 1 if catalog coverage is below 80%.

## Synonym sources (catalog coverage)

Used by `scripts/refresh_gi_rankings.py` to build denser `aliases` (proxy unchanged).

| Source | Live URL / path | Offline default |
|--------|-----------------|-----------------|
| OpenRouter | `https://openrouter.ai/api/v1/models` | `data/gi_sources/openrouter_models.json` |
| LiteLLM | BerriAI `model_prices_and_context_window.json` (raw GitHub) | `data/gi_sources/litellm_prices.json` |
| llm plugin aliases | checked-in map | `data/gi_sources/llm_plugin_aliases.json` |

```bash
# reproducible (no network)
python scripts/refresh_gi_rankings.py \
  --lmsys data/gi_sources/lmsys.json \
  --aa data/gi_sources/aa.json \
  --catalog data/gi_sources/catalog.json \
  --offline \
  --openrouter data/gi_sources/openrouter_models.json \
  --litellm data/gi_sources/litellm_prices.json \
  --out gi_rankings.json

# live fetch OpenRouter + LiteLLM (default when not --offline)
python scripts/refresh_gi_rankings.py \
  --lmsys data/gi_sources/lmsys.json \
  --catalog data/gi_sources/catalog.json \
  --llm \
  --out gi_rankings.json
```

Match order: deterministic → synonym graph → optional `--llm`.
Missing `llm_plugin_aliases.json` is treated as empty. `--offline` fails if OpenRouter or LiteLLM files are missing.
Do not commit huge live dumps unless intentionally refreshing cache files.
