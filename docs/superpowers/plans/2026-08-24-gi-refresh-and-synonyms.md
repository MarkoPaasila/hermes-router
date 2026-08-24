# GI Refresh 2026-08-24 + Synonym Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship maintainer synonym-graph aliases, then refresh `gi_rankings.json` and `data/gi_sources/` from Arena 2026-08-24, Artificial Analysis (`AA_API_KEY`), OpenRouter, and LiteLLM — with LMSYS/seed overlay so catalog coverage stays complete.

**Architecture:** Implement `scripts/gi_synonyms.py` and wire it into `refresh_gi_rankings.py` (exact steps in the Aug 10 synonym plan). Add `apply_seed_overlay` + LMSYS Elo merge helpers. Fetch/convert sources into `data/gi_sources/`, rebuild `catalog.json`, run offline refresh, commit synonym code then data separately. Leave uncommitted specialized-filter WIP untouched.

**Tech Stack:** Python 3, pytest, stdlib `urllib`/`json`/`argparse`, existing `gi_ranking` helpers.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-24-gi-refresh-and-synonyms-design.md`
- Synonym behavior: `docs/superpowers/specs/2026-08-10-gi-synonym-matching-design.md`
- Synonym code/tests: execute verbatim from `docs/superpowers/plans/2026-08-10-gi-synonym-matching.md` Tasks 1–5
- Proxy / `gi_ranking.py` public matching unchanged
- Never invent snapshot keys or GI scores
- LMSYS Elo overlay retains prior `lmsys.json` ids; seed post-pass from prior `gi_rankings.json`
- AA: fresh export only via `AA_API_KEY`
- Coverage floor 80%; aim ~100% via overlay + aliases
- Do not stage specialized-filter WIP (`router.py`, `documentation/configuration.md`, `tests/test_specialized_models.py`)
- `docs/superpowers/` is gitignored — use `git add -f` for plans/specs under that tree

## File Structure

| File | Responsibility |
|------|----------------|
| `scripts/gi_synonyms.py` | Synonym graph, fetch/load, resolve aliases |
| `scripts/refresh_gi_rankings.py` | CLI, synonym wiring, seed overlay helper |
| `tests/test_gi_synonyms.py` | Synonym unit tests (no network) |
| `tests/test_refresh_gi_rankings.py` | Refresh integration + overlay tests |
| `data/gi_sources/llm_plugin_aliases.json` | Plugin short↔full map (`{"aliases":{}}`) |
| `data/gi_sources/README.md` | Fetch URLs, `--offline`, overlay note |
| `data/gi_sources/*` | LMSYS, AA, OpenRouter, LiteLLM, catalog, pointer |
| `gi_rankings.json` | Checked-in GI snapshot |

---

### Task 1: Synonym graph core + edges + resolve (Aug 10 Tasks 1–3)

**Files:**
- Create: `scripts/gi_synonyms.py`
- Create: `tests/test_gi_synonyms.py`
- Create: `data/gi_sources/llm_plugin_aliases.json`

**Interfaces:**
- Produces (must exist after this task):
  - `strip_calendar_suffix(s: str) -> str`
  - `class SynonymGraph` with `add`, `component`, `trusted_pair`
  - `load_plugin_aliases(path: Path | None) -> dict[str, str]`
  - `load_json_file`, `fetch_json`, `add_openrouter_edges`, `add_litellm_edges`, `add_plugin_alias_edges`, `build_synonym_graph`
  - `sibling_tokens`, `allows_synonym_target`, `resolve_via_synonyms`, `resolve_catalog_aliases`
  - `DEFAULT_OPENROUTER_PATH`, `DEFAULT_LITELLM_PATH`, `DEFAULT_PLUGIN_ALIASES_PATH`, `OPENROUTER_URL`, `LITELLM_URL`
- Consumes: `gi_ranking.normalize_model_id`, `modality_tokens`; `refresh_gi_rankings.deterministic_match` as injectable `match_fn`

- [ ] **Step 1:** Open `docs/superpowers/plans/2026-08-10-gi-synonym-matching.md` and execute **Task 1** completely (failing tests → implement → pass → commit `feat: add GI synonym graph core helpers`).

- [ ] **Step 2:** Execute that plan’s **Task 2** completely (commit `feat: build GI synonym edges from OpenRouter and LiteLLM`).

- [ ] **Step 3:** Execute that plan’s **Task 3** completely (commit `feat: resolve GI catalog aliases via synonym graph`).

- [ ] **Step 4: Verify**

Run: `pytest tests/test_gi_synonyms.py -v`

Expected: PASS (no network).

---

### Task 2: Wire synonyms into refresh + README (Aug 10 Tasks 4–5)

**Files:**
- Modify: `scripts/refresh_gi_rankings.py`
- Modify: `tests/test_refresh_gi_rankings.py`
- Modify: `data/gi_sources/README.md`

**Interfaces:**
- Extends `run_refresh(...)` with: `openrouter`, `litellm`, `llm_aliases`, `offline`, `openrouter_payload`, `litellm_payload`, `fetch_openrouter`, `fetch_litellm`
- Coverage may include `via: {deterministic, synonym, llm}`
- CLI: `--openrouter`, `--litellm`, `--llm-aliases`, `--offline`
- Match order: deterministic → synonym → optional `--llm`
- Existing `test_build_doc_includes_aliases_and_coverage` must pass `offline=True`, `openrouter_payload={}`, `litellm_payload={}` so CI never hits the network

- [ ] **Step 1:** Execute Aug 10 plan **Task 4** completely (commit `feat: wire synonym graph into GI rankings refresh`).

- [ ] **Step 2:** Execute Aug 10 plan **Task 5** completely (commit `docs: document GI synonym source fetch and offline paths`).

- [ ] **Step 3: Verify**

Run: `pytest tests/test_refresh_gi_rankings.py tests/test_gi_synonyms.py tests/test_gi_ranking.py -q`

Expected: PASS.

---

### Task 3: Seed overlay helper

**Files:**
- Modify: `scripts/refresh_gi_rankings.py`
- Modify: `tests/test_refresh_gi_rankings.py`

**Interfaces:**
- Produces:
  - `apply_seed_overlay(models: dict[str, dict], prior_snapshot: dict | None) -> dict[str, dict]`
  - Behavior: for each id in `prior_snapshot["models"]` whose `sources` keys are exactly `{"seed"}` (or only `seed`), if that id is **absent** from `models`, copy `{ "gi": <prior gi>, "sources": {"seed": <prior gi>} }` into `models`. Do not overwrite ids already present from lmsys/aa.
- Consumes: snapshot shape from `gi_rankings.json`

- [ ] **Step 1: Write failing tests**

```python
def test_apply_seed_overlay_retains_seed_only_ids():
    models = {
        "gpt-4o": {"gi": 90.0, "sources": {"lmsys": 90.0}},
    }
    prior = {
        "models": {
            "gpt-4o": {"gi": 88.0, "sources": {"lmsys": 88.0}},
            "big-pickle": {"gi": 72.0, "sources": {"seed": 72.0}},
            "old-lmsys-only": {"gi": 50.0, "sources": {"lmsys": 50.0}},
        }
    }
    out = refresh.apply_seed_overlay(models, prior)
    assert out["gpt-4o"]["gi"] == 90.0  # not overwritten
    assert out["big-pickle"] == {"gi": 72.0, "sources": {"seed": 72.0}}
    assert "old-lmsys-only" not in out  # only seed-only retained here


def test_apply_seed_overlay_none_prior_noop():
    models = {"a": {"gi": 1.0, "sources": {"lmsys": 1.0}}}
    assert refresh.apply_seed_overlay(models, None) == models
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/test_refresh_gi_rankings.py::test_apply_seed_overlay_retains_seed_only_ids -v`

Expected: FAIL (`apply_seed_overlay` missing).

- [ ] **Step 3: Implement**

```python
def apply_seed_overlay(
    models: dict[str, dict],
    prior_snapshot: dict | None,
) -> dict[str, dict]:
    if not prior_snapshot or not isinstance(prior_snapshot.get("models"), dict):
        return models
    out = dict(models)
    for mid, entry in prior_snapshot["models"].items():
        if mid in out:
            continue
        if not isinstance(entry, dict):
            continue
        src = entry.get("sources") or {}
        if set(src.keys()) != {"seed"}:
            continue
        try:
            gi = float(entry.get("gi", src.get("seed")))
        except (TypeError, ValueError):
            continue
        out[mid] = {"gi": round(gi, 2), "sources": {"seed": round(gi, 2)}}
    return out
```

Call from `run_refresh` after `build_models_from_sources` when optional kwarg `prior_snapshot: dict | None = None` is set (or `prior_snapshot_path: Path | None`). Prefer:

```python
# in run_refresh signature:
prior_snapshot: dict | None = None,
prior_snapshot_path: Path | None = None,
# load path if dict not given:
if prior_snapshot is None and prior_snapshot_path and prior_snapshot_path.exists():
    prior_snapshot = json.loads(prior_snapshot_path.read_text())
models = build_models_from_sources(present)
models = apply_seed_overlay(models, prior_snapshot)
# then rebuild known_keys from models
```

CLI: `--prior PATH` defaulting to `ROOT / "gi_rankings.json"` when the file exists; pass `None` if missing. Include `"seed"` in top-level `sources` list when any model has a seed source. Accept optional `note: str | None` kwarg written into the doc when provided.

- [ ] **Step 4: Run tests — expect PASS**

Run: `pytest tests/test_refresh_gi_rankings.py -q`

Expected: PASS (update existing tests to pass `offline=True`, empty OR/LiteLLM payloads if Task 2 already requires that).

- [ ] **Step 5: Commit**

```bash
git add scripts/refresh_gi_rankings.py tests/test_refresh_gi_rankings.py
git commit -m "$(cat <<'EOF'
feat: retain seed-only GI scores across ranking refresh

EOF
)"
```

---

### Task 4: Fetch Arena + AA + OpenRouter + LiteLLM and convert

**Files:**
- Update (write): `data/gi_sources/latest_ptr.json`, `lmsys_text.json`, `lmsys_code.json`, `lmsys_raw.json`, `from_lmsys.json`, `lmsys.json`, `aa_raw.json`, `aa.json`, `openrouter_models.json`, `litellm_prices.json`

**Interfaces:** none new — maintainer shell/Python one-shot in-repo (do not add a permanent fetch CLI unless already exists).

- [ ] **Step 1: Confirm Arena date and download boards**

```bash
cd /home/marko/projektit/Hermes-router
curl -fsSL -o /tmp/arena_latest.json \
  "https://raw.githubusercontent.com/oolong-tea-2026/arena-ai-leaderboards/main/data/latest.json"
python3 -c "import json; print(json.load(open('/tmp/arena_latest.json')))"
# Expect path/date 2026-08-24
DATE=2026-08-24
curl -fsSL -o data/gi_sources/lmsys_text.json \
  "https://raw.githubusercontent.com/oolong-tea-2026/arena-ai-leaderboards/main/data/${DATE}/text.json"
curl -fsSL -o data/gi_sources/lmsys_code.json \
  "https://raw.githubusercontent.com/oolong-tea-2026/arena-ai-leaderboards/main/data/${DATE}/code.json"
printf '%s\n' "{\"date\": \"${DATE}\", \"path\": \"${DATE}\"}" > data/gi_sources/latest_ptr.json
```

- [ ] **Step 2: Build new board Elo + overlay onto prior `lmsys.json`**

```python
# run via python3 <<'PY' ...
import json
from pathlib import Path
from collections import defaultdict

src = Path("data/gi_sources")
text = json.loads((src / "lmsys_text.json").read_text())
code = json.loads((src / "lmsys_code.json").read_text())

def board_scores(doc):
    models = doc.get("models") if isinstance(doc, dict) else doc
    out = {}
    for m in models or []:
        mid = (m.get("model") or m.get("id") or "").strip().lower()
        if not mid:
            continue
        out[mid] = float(m["score"])
    return out

t, c = board_scores(text), board_scores(code)
ids = set(t) | set(c)
fresh = {}
for mid in ids:
    vals = [v for v in (t.get(mid), c.get(mid)) if v is not None]
    fresh[mid] = sum(vals) / len(vals)

prior = {e["id"].lower(): float(e["score"]) for e in json.loads((src / "lmsys.json").read_text())}
merged = dict(prior)
merged.update(fresh)  # new wins on conflict

(src / "from_lmsys.json").write_text(json.dumps(
    [{"id": k, "score": v} for k, v in sorted(fresh.items())], indent=2) + "\n")
(src / "lmsys.json").write_text(json.dumps(
    [{"id": k, "score": v} for k, v in sorted(merged.items())], indent=2) + "\n")
(src / "lmsys_raw.json").write_text(json.dumps({
    "date": json.loads((src / "latest_ptr.json").read_text())["date"],
    "text_n": len(t), "code_n": len(c), "fresh_n": len(fresh), "merged_n": len(merged),
}, indent=2) + "\n")
print("fresh", len(fresh), "merged", len(merged))
```

- [ ] **Step 3: Fetch Artificial Analysis**

Require `AA_API_KEY` in the environment (abort if unset).

Try in order until HTTP 200 with a model list:

1. `GET https://artificialanalysis.ai/api/v2/data/llms/models` with header `x-api-key: $AA_API_KEY`
2. Same URL with `Authorization: Bearer $AA_API_KEY`
3. `GET https://artificialanalysis.ai/api/v2/language/models` with the same two auth styles

Shape `aa.json` as `[{ "id": <slug>, "score": <artificial_analysis_intelligence_index> }, ...]` using the same field path as prior `aa_raw.json` (`data[].slug` + `data[].evaluations.artificial_analysis_intelligence_index`). Write full response to `aa_raw.json`. Skip entries missing slug or index.

```bash
test -n "$AA_API_KEY" || { echo "AA_API_KEY unset"; exit 1; }
# implement fetch in a short python script; on failure print status body and exit 1
```

- [ ] **Step 4: Fetch OpenRouter + LiteLLM caches**

```bash
curl -fsSL -o data/gi_sources/openrouter_models.json \
  "https://openrouter.ai/api/v1/models"
curl -fsSL -o data/gi_sources/litellm_prices.json \
  "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
python3 -c "import json; o=json.load(open('data/gi_sources/openrouter_models.json')); print('or', len(o.get('data',o))); lt=json.load(open('data/gi_sources/litellm_prices.json')); print('lt', len(lt))"
```

- [ ] **Step 5: Do not commit yet** — sources are consumed by Task 5.

---

### Task 5: Rebuild catalog, run refresh, commit data

**Files:**
- Modify: `data/gi_sources/catalog.json`
- Modify: `gi_rankings.json`
- Possibly touch: `data/gi_sources/README.md` (one line noting Arena 2026-08-24 overlay)

**Interfaces:** uses Task 2–3 CLI.

- [ ] **Step 1: Union catalog with OpenRouter chat ids**

```python
import json
from pathlib import Path
src = Path("data/gi_sources")
prior = json.loads((src / "catalog.json").read_text())
or_doc = json.loads((src / "openrouter_models.json").read_text())
items = or_doc.get("data") if isinstance(or_doc, dict) else or_doc
or_ids = []
for it in items or []:
    if not isinstance(it, dict):
        continue
    mid = (it.get("id") or "").strip()
    if not mid:
        continue
    arch = it.get("architecture") or {}
    # Prefer chat-capable: text on output, or modality containing '->text' / output text
    modality = str(arch.get("modality") or "").lower()
    outs = arch.get("output_modalities") or []
    out_text = any(str(x).lower() == "text" for x in outs) if isinstance(outs, list) else False
    if out_text or "->text" in modality or "→text" in modality or not arch:
        # if no arch, keep id (broad union); specialty GI guards handle non-chat
        or_ids.append(mid)
catalog = sorted(set(prior) | set(or_ids), key=str.lower)
(src / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
print("catalog", len(prior), "->", len(catalog))
```

- [ ] **Step 2: Run offline refresh with prior snapshot + note**

```bash
NOTE='LMSYS Arena 2026-08-24 text+code Elo (mean of boards) overlaid onto prior lmsys.json; AA export 2026-08-24; seed retained for seed-only prior ids; OpenRouter+LiteLLM synonym aliases.'
python scripts/refresh_gi_rankings.py \
  --lmsys data/gi_sources/lmsys.json \
  --aa data/gi_sources/aa.json \
  --catalog data/gi_sources/catalog.json \
  --offline \
  --openrouter data/gi_sources/openrouter_models.json \
  --litellm data/gi_sources/litellm_prices.json \
  --prior gi_rankings.json \
  --out gi_rankings.json \
  --note "$NOTE"
```

If `--note` / `--prior` were not added in Task 3, set them in code before this step (do not skip).

Expected: exit 0; print coverage ≥ 80% (aim ~100%). If exit 1, inspect unmatched ids; fix aliases or extend seed only with justification — do not lower the floor silently.

- [ ] **Step 3: Sanity-check snapshot**

```python
import json
g = json.load(open("gi_rankings.json"))
assert "seed" in g.get("sources", []) or any(
    set((v.get("sources") or {})) == {"seed"} for v in g["models"].values()
)
print(g["generated_at"], g.get("note"), g.get("coverage"), len(g["models"]), len(g.get("aliases", {})))
```

- [ ] **Step 4: Commit data only**

```bash
git add data/gi_sources/gi_rankings.json 2>/dev/null
git add data/gi_sources/*.json gi_rankings.json data/gi_sources/README.md
# Do NOT add router.py / specialized-filter WIP
git status
git commit -m "$(cat <<'EOF'
chore: refresh GI snapshot from Arena 2026-08-24 and Artificial Analysis

EOF
)"
```

Fix the `git add` line: add `gi_rankings.json` and `data/gi_sources/` caches intentionally refreshed (`lmsys*`, `aa*`, `openrouter_models.json`, `litellm_prices.json`, `catalog.json`, `latest_ptr.json`, `from_lmsys.json`, `llm_plugin_aliases.json` if changed). Omit unrelated dirty files.

- [ ] **Step 5: Final verification**

Run: `pytest tests/test_gi_synonyms.py tests/test_refresh_gi_rankings.py tests/test_gi_ranking.py -q`

Expected: PASS.

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Ship `gi_synonyms.py` + Aug 10 behavior | 1–2 |
| Wire `--offline` / OR / LiteLLM / synonym before `--llm` | 2 |
| Arena 2026-08-24 text+code mean Elo | 4 |
| LMSYS overlay retain prior ids | 4 |
| AA via `AA_API_KEY` | 4 |
| OpenRouter + LiteLLM caches | 4 |
| Catalog ∪ OpenRouter chat ids | 5 |
| Seed post-pass | 3, 5 |
| Coverage ≥ 80%, accurate note | 5 |
| Two commit groups (code then data) | 1–3 commits + Task 5 data commit |
| Leave specialized-filter WIP alone | Global Constraints + Task 5 |
| Tests offline / no network in CI | 1–3, 5 |

No TBD placeholders. Interfaces aligned: `apply_seed_overlay`, `run_refresh(..., prior_snapshot=..., note=...)`, synonym APIs from Aug 10 plan.
