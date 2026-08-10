# GI Synonym Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise GI catalog coverage by building a maintainer-only synonym graph from OpenRouter, LiteLLM, and `llm-*` plugin aliases, then writing denser `aliases` into `gi_rankings.json` without changing proxy matching.

**Architecture:** New `scripts/gi_synonyms.py` loads/fetches the three sources, unions explicit name edges, and resolves each catalog id to a snapshot key via existing `deterministic_match` over the connected component (with sibling/modality guards). `scripts/refresh_gi_rankings.py` calls it between deterministic and optional `--llm` steps. Live fetch is default; `--offline` uses files under `data/gi_sources/`.

**Tech Stack:** Python 3, pytest, stdlib `urllib` / `json` / `argparse` (same as current refresh script).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-10-gi-synonym-matching-design.md`
- Proxy / `gi_ranking.py` public matching behavior unchanged
- Never invent snapshot keys or GI scores
- No mini/lite/flash sibling collapse via graph alone (trusted edges: `alias_target`, shared HF id only)
- Modality-token safety via existing `gi_ranking` helpers
- `--llm` remains last resort after synonym matching
- Live OpenRouter + LiteLLM by default; `--offline` requires those two files
- Missing `llm_plugin_aliases.json` → warn + empty map (do not fail)

## File Structure

| File | Responsibility |
|------|----------------|
| `scripts/gi_synonyms.py` | Fetch/load sources, union-find graph, edge builders, `resolve_catalog_aliases` |
| `scripts/refresh_gi_rankings.py` | CLI flags, wire synonym step into `run_refresh`, coverage path counts |
| `tests/test_gi_synonyms.py` | Unit tests for graph/edges/safety (fixtures, no network) |
| `tests/test_refresh_gi_rankings.py` | Refresh integration: offline, synonym-before-llm, coverage |
| `data/gi_sources/llm_plugin_aliases.json` | Checked-in plugin short↔full map (start as `{}` or small seed) |
| `data/gi_sources/README.md` | Document new sources, URLs, `--offline` |

---

### Task 1: Date-strip helper + union-find + plugin alias loader

**Files:**
- Create: `scripts/gi_synonyms.py`
- Create: `tests/test_gi_synonyms.py`
- Create: `data/gi_sources/llm_plugin_aliases.json`

**Interfaces:**
- Produces:
  - `strip_calendar_suffix(s: str) -> str`
  - `class SynonymGraph` with `add(a: str, b: str, *, trusted: bool = False) -> None`, `component(name: str) -> set[str]`, `trusted_pair(a: str, b: str) -> bool`
  - `load_plugin_aliases(path: Path | None) -> dict[str, str]` (normalized short → full id string; missing file → `{}`)
  - `DEFAULT_PLUGIN_ALIASES_PATH: Path` → `ROOT/data/gi_sources/llm_plugin_aliases.json`
- Consumes: `gi_ranking.normalize_model_id`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_gi_synonyms.py
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import gi_synonyms as syn  # noqa: E402


def test_strip_calendar_suffix():
    assert syn.strip_calendar_suffix("qwen/qwen3.8-max-20260803") == "qwen/qwen3.8-max"
    assert syn.strip_calendar_suffix("deepseek/deepseek-v4-flash-20260731") == "deepseek/deepseek-v4-flash"
    assert syn.strip_calendar_suffix("google/gemini-2.5-flash") == "google/gemini-2.5-flash"
    assert syn.strip_calendar_suffix("model-2026-08-03") == "model"


def test_synonym_graph_connects_transitive():
    g = syn.SynonymGraph()
    g.add("a", "b")
    g.add("b", "c")
    assert g.component("a") == {"a", "b", "c"}


def test_synonym_graph_trusted_pair():
    g = syn.SynonymGraph()
    g.add("x", "y", trusted=True)
    g.add("y", "z")  # untrusted
    assert g.trusted_pair("x", "y") is True
    assert g.trusted_pair("y", "z") is False


def test_load_plugin_aliases_missing_is_empty(tmp_path):
    assert syn.load_plugin_aliases(tmp_path / "missing.json") == {}


def test_load_plugin_aliases_map(tmp_path):
    p = tmp_path / "aliases.json"
    p.write_text(json.dumps({
        "aliases": {
            "flash": "gemini-2.5-flash",
            "OpenAI/GPT-4o": "gpt-4o",
        }
    }))
    got = syn.load_plugin_aliases(p)
    assert got["flash"] == "gemini-2.5-flash"
    assert "gpt-4o" in got.values() or got.get("gpt-4o") == "gpt-4o" or "openai/gpt-4o" in got
```

For `load_plugin_aliases`, accept either `{"aliases": {short: full, ...}}` or a flat `{short: full}` object. Normalize keys with `gi_ranking.normalize_model_id` (fallback lowercased strip). Store values as the original full id string (lowercased strip), and also add an edge-ready pair later in Task 2/3.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gi_synonyms.py::test_strip_calendar_suffix tests/test_gi_synonyms.py::test_synonym_graph_connects_transitive tests/test_gi_synonyms.py::test_load_plugin_aliases_missing_is_empty -v`

Expected: FAIL with `ModuleNotFoundError` or import error for `gi_synonyms`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/gi_synonyms.py`:

```python
"""Synonym graph for GI catalog↔snapshot matching (maintainer refresh only)."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

import gi_ranking  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_PLUGIN_ALIASES_PATH = ROOT / "data" / "gi_sources" / "llm_plugin_aliases.json"
DEFAULT_OPENROUTER_PATH = ROOT / "data" / "gi_sources" / "openrouter_models.json"
DEFAULT_LITELLM_PATH = ROOT / "data" / "gi_sources" / "litellm_prices.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

_CAL_SUFFIX_RE = re.compile(r"-(?:20\d{6}|\d{4}-\d{2}-\d{2})$")


def strip_calendar_suffix(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    return _CAL_SUFFIX_RE.sub("", s)


def _norm_node(s: str) -> str:
    return gi_ranking.normalize_model_id(s) or (s or "").strip().lower()


class SynonymGraph:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._trusted_edges: set[frozenset[str]] = set()

    def _find(self, x: str) -> str:
        p = self._parent.setdefault(x, x)
        if p != x:
            self._parent[x] = self._find(p)
        return self._parent[x]

    def add(self, a: str, b: str, *, trusted: bool = False) -> None:
        na, nb = _norm_node(a), _norm_node(b)
        if not na or not nb:
            return
        # Always register both nodes even if equal
        self._parent.setdefault(na, na)
        self._parent.setdefault(nb, nb)
        if na == nb:
            return
        ra, rb = self._find(na), self._find(nb)
        if ra != rb:
            self._parent[rb] = ra
        if trusted:
            self._trusted_edges.add(frozenset((na, nb)))

    def component(self, name: str) -> set[str]:
        n = _norm_node(name)
        if not n or n not in self._parent:
            return {n} if n else set()
        root = self._find(n)
        return {k for k in self._parent if self._find(k) == root}

    def trusted_pair(self, a: str, b: str) -> bool:
        na, nb = _norm_node(a), _norm_node(b)
        return frozenset((na, nb)) in self._trusted_edges


def load_plugin_aliases(path: Path | None) -> dict[str, str]:
    """Return normalized_short -> full_id (lowercased). Missing → {}."""
    p = path
    if p is None:
        return {}
    if not p.exists():
        log.warning("[gi-synonyms] plugin aliases missing at %s — treating as empty", p)
        return {}
    try:
        doc = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[gi-synonyms] bad plugin aliases %s: %s", p, e)
        return {}
    raw = doc.get("aliases") if isinstance(doc, dict) and "aliases" in doc else doc
    if not isinstance(raw, dict):
        log.warning("[gi-synonyms] plugin aliases not a map in %s", p)
        return {}
    out: dict[str, str] = {}
    for src, dst in raw.items():
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        sk = _norm_node(src)
        tv = dst.strip().lower()
        if sk and tv:
            out[sk] = tv
    return out
```

Create `data/gi_sources/llm_plugin_aliases.json`:

```json
{
  "aliases": {}
}
```

Fix the plugin-alias test to assert exact normalized behavior:

```python
def test_load_plugin_aliases_map(tmp_path):
    p = tmp_path / "aliases.json"
    p.write_text(json.dumps({"aliases": {"flash": "gemini-2.5-flash", "GPT-4o": "openai/gpt-4o"}}))
    got = syn.load_plugin_aliases(p)
    assert got["flash"] == "gemini-2.5-flash"
    assert got["gpt-4o"] == "openai/gpt-4o"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gi_synonyms.py -v`

Expected: PASS for the five tests above.

- [ ] **Step 5: Commit**

```bash
git add -f scripts/gi_synonyms.py tests/test_gi_synonyms.py data/gi_sources/llm_plugin_aliases.json
git commit -m "$(cat <<'EOF'
feat: add GI synonym graph core helpers

EOF
)"
```

---

### Task 2: Build graph from OpenRouter + LiteLLM + plugin maps

**Files:**
- Modify: `scripts/gi_synonyms.py`
- Modify: `tests/test_gi_synonyms.py`

**Interfaces:**
- Consumes: Task 1 `SynonymGraph`, `strip_calendar_suffix`, `load_plugin_aliases`, `_norm_node`
- Produces:
  - `load_json_file(path: Path) -> object | None` (warn + None on bad/missing)
  - `fetch_json(url: str, timeout: float = 60.0) -> object` (raises on failure)
  - `add_openrouter_edges(graph: SynonymGraph, payload: object) -> None`
  - `add_litellm_edges(graph: SynonymGraph, payload: object) -> None`
  - `add_plugin_alias_edges(graph: SynonymGraph, aliases: dict[str, str]) -> None`
  - `build_synonym_graph(*, openrouter: object | None, litellm: object | None, plugin_aliases: dict[str, str]) -> SynonymGraph`

OpenRouter payload shape: `{"data": [ {id, canonical_slug, hugging_face_id, alias_target?}, ... ]}` or a bare list.

LiteLLM payload: top-level object mapping model key → `{mode, ...}`. Only include entries where `mode == "chat"` (skip `sample_spec` and non-dicts).

- [ ] **Step 1: Write the failing tests**

```python
def test_add_openrouter_edges_alias_target_and_hf():
    g = syn.SynonymGraph()
    payload = {
        "data": [
            {
                "id": "~google/gemini-flash-latest",
                "canonical_slug": "~google/gemini-flash-latest",
                "hugging_face_id": None,
                "alias_target": {"slug": "google/gemini-3.6-flash"},
            },
            {
                "id": "deepseek/deepseek-v4-flash-0731",
                "canonical_slug": "deepseek/deepseek-v4-flash-20260731",
                "hugging_face_id": "deepseek-ai/DeepSeek-V4-Flash-0731",
            },
            {
                "id": "other/deepseek-v4-flash-alt",
                "canonical_slug": "other/deepseek-v4-flash-alt",
                "hugging_face_id": "deepseek-ai/DeepSeek-V4-Flash-0731",
            },
        ]
    }
    syn.add_openrouter_edges(g, payload)
    # latest ↔ concrete via trusted alias_target
    assert "gemini-3.6-flash" in g.component("~google/gemini-flash-latest")
    assert g.trusted_pair("~google/gemini-flash-latest", "google/gemini-3.6-flash")
    # shared HF links the two deepseek spellings
    assert "deepseek-v4-flash-alt" in g.component("deepseek/deepseek-v4-flash-0731")
    # canonical date-stripped links to id
    assert "deepseek-v4-flash" in g.component("deepseek/deepseek-v4-flash-0731") or \
        "deepseek-v4-flash-20260731" in g.component("deepseek/deepseek-v4-flash-0731")


def test_add_litellm_edges_strips_provider_prefix():
    g = syn.SynonymGraph()
    payload = {
        "gemini/gemini-2.5-flash": {"mode": "chat", "litellm_provider": "gemini"},
        "openai/gpt-4o": {"mode": "chat"},
        "text-embedding-3-small": {"mode": "embedding"},
        "sample_spec": {"mode": "chat"},
    }
    syn.add_litellm_edges(g, payload)
    assert "gemini-2.5-flash" in g.component("gemini/gemini-2.5-flash")
    assert "gpt-4o" in g.component("openai/gpt-4o")
    # embedding skipped — node may be absent
    assert g.component("text-embedding-3-small") == {"text-embedding-3-small"} or \
        "text-embedding-3-small" not in g._parent


def test_add_plugin_alias_edges():
    g = syn.SynonymGraph()
    syn.add_plugin_alias_edges(g, {"flash": "gemini-2.5-flash"})
    assert "gemini-2.5-flash" in g.component("flash")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gi_synonyms.py::test_add_openrouter_edges_alias_target_and_hf tests/test_gi_synonyms.py::test_add_litellm_edges_strips_provider_prefix tests/test_gi_synonyms.py::test_add_plugin_alias_edges -v`

Expected: FAIL with `AttributeError` / not defined.

- [ ] **Step 3: Implement edge builders**

Append to `scripts/gi_synonyms.py`:

```python
import urllib.error
import urllib.request


def load_json_file(path: Path) -> object | None:
    if not path.exists():
        log.warning("[gi-synonyms] missing JSON file %s", path)
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("[gi-synonyms] could not read %s: %s", path, e)
        return None


def fetch_json(url: str, timeout: float = 60.0) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-router-gi-refresh"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def add_openrouter_edges(graph: SynonymGraph, payload: object) -> None:
    if payload is None:
        return
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        log.warning("[gi-synonyms] openrouter payload not a list")
        return
    by_hf: dict[str, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = (item.get("id") or "").strip()
        if not mid:
            continue
        graph.add(mid, mid)
        slug = (item.get("canonical_slug") or "").strip()
        if slug:
            graph.add(mid, slug)
            stripped = strip_calendar_suffix(slug)
            if stripped and stripped != slug:
                graph.add(mid, stripped)
        at = item.get("alias_target")
        if isinstance(at, dict):
            target = (at.get("slug") or "").strip()
            if target:
                graph.add(mid, target, trusted=True)
        hf = item.get("hugging_face_id")
        if isinstance(hf, str) and hf.strip():
            by_hf.setdefault(hf.strip(), []).append(mid)
    for _hf, ids in by_hf.items():
        for i in range(1, len(ids)):
            graph.add(ids[0], ids[i], trusted=True)


def add_litellm_edges(graph: SynonymGraph, payload: object) -> None:
    if not isinstance(payload, dict):
        if payload is not None:
            log.warning("[gi-synonyms] litellm payload not an object")
        return
    for key, meta in payload.items():
        if key == "sample_spec" or not isinstance(key, str):
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get("mode") != "chat":
            continue
        graph.add(key, key)
        if "/" in key:
            graph.add(key, key.rsplit("/", 1)[-1])


def add_plugin_alias_edges(graph: SynonymGraph, aliases: dict[str, str]) -> None:
    for short, full in aliases.items():
        graph.add(short, full)


def build_synonym_graph(
    *,
    openrouter: object | None,
    litellm: object | None,
    plugin_aliases: dict[str, str],
) -> SynonymGraph:
    g = SynonymGraph()
    add_openrouter_edges(g, openrouter)
    add_litellm_edges(g, litellm)
    add_plugin_alias_edges(g, plugin_aliases)
    return g
```

Tighten the LiteLLM test: after `add_litellm_edges`, assert `"text-embedding-3-small" not in g._parent`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_gi_synonyms.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gi_synonyms.py tests/test_gi_synonyms.py
git commit -m "$(cat <<'EOF'
feat: build GI synonym edges from OpenRouter and LiteLLM

EOF
)"
```

---

### Task 3: Resolve catalog ids via synonyms with sibling safety

**Files:**
- Modify: `scripts/gi_synonyms.py`
- Modify: `tests/test_gi_synonyms.py`

**Interfaces:**
- Consumes: `refresh_gi_rankings.deterministic_match` (import from refresh module, or pass matcher callable to avoid cycles — prefer injectable `match_fn`)
- Produces:
  - `SIBLING_TOKENS = frozenset({"mini", "lite", "flash"})`
  - `sibling_tokens(model_id: str) -> frozenset[str]`
  - `allows_synonym_target(catalog_id: str, snapshot_key: str, graph: SynonymGraph) -> bool`
  - `resolve_via_synonyms(catalog_id: str, known_keys: set[str], graph: SynonymGraph, match_fn=...) -> str | None`
  - `resolve_catalog_aliases(catalog_ids: list[str], known_keys: set[str], graph: SynonymGraph, models: set[str], match_fn=...) -> dict[str, str]`

**Sibling / modality rule (contract = tests below):**

- Refuse if catalog has modality tokens the snapshot key lacks.
- If catalog has extra sibling tokens (`mini`/`lite`/`flash`) vs the snapshot key, accept only when a **trusted** edge relates catalog↔synonym, catalog↔key, or synonym↔key.
- Among multiple candidate keys: longest key wins; ties → lexicographically smaller.

- [ ] **Step 1: Write the failing tests**

```python
import refresh_gi_rankings as refresh


def test_resolve_via_synonyms_alias_target():
    g = syn.SynonymGraph()
    syn.add_openrouter_edges(g, {"data": [{
        "id": "vendor/cool-model-latest",
        "canonical_slug": "vendor/cool-model-latest",
        "alias_target": {"slug": "vendor/cool-model-v2"},
    }]})
    known = {"cool-model-v2"}
    hit = syn.resolve_via_synonyms(
        "vendor/cool-model-latest", known, g, match_fn=refresh.deterministic_match
    )
    assert hit == "cool-model-v2"


def test_resolve_via_synonyms_blocks_mini_to_base_without_trusted_edge():
    g = syn.SynonymGraph()
    # Untrusted link only (both nodes present via identity-style add through a shared bogus bridge)
    g.add("gpt-4o-mini", "gpt-4o-bridge")
    g.add("gpt-4o", "gpt-4o-bridge")
    known = {"gpt-4o"}
    assert syn.resolve_via_synonyms(
        "openai/gpt-4o-mini", known, g, match_fn=refresh.deterministic_match
    ) is None


def test_resolve_via_synonyms_allows_mini_with_trusted_hf_or_alias():
    g = syn.SynonymGraph()
    g.add("gpt-4o-mini", "gpt-4o", trusted=True)
    known = {"gpt-4o"}
    assert syn.resolve_via_synonyms(
        "openai/gpt-4o-mini", known, g, match_fn=refresh.deterministic_match
    ) == "gpt-4o"


def test_resolve_catalog_aliases_skips_exact_deterministic_already_matched():
    # resolve_catalog_aliases only emits aliases when norm != hit and hit in models
    g = syn.SynonymGraph()
    syn.add_plugin_alias_edges(g, {"deepseek-chat-v3": "deepseek-v3"})
    aliases = syn.resolve_catalog_aliases(
        ["deepseek/deepseek-chat-v3", "gpt-4o"],
        known_keys={"deepseek-v3", "gpt-4o"},
        graph=g,
        models={"deepseek-v3", "gpt-4o"},
        match_fn=refresh.deterministic_match,
    )
    assert aliases.get("deepseek-chat-v3") == "deepseek-v3"
    # gpt-4o exact — may omit alias when norm == hit
    assert "gpt-4o" not in aliases or aliases["gpt-4o"] == "gpt-4o"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gi_synonyms.py::test_resolve_via_synonyms_alias_target tests/test_gi_synonyms.py::test_resolve_via_synonyms_blocks_mini_to_base_without_trusted_edge -v`

Expected: FAIL (functions missing).

- [ ] **Step 3: Implement resolve helpers**

```python
_SIBLING_RE = re.compile(r"(^|[-_])(mini|lite|flash)([-_]|$)", re.I)


def sibling_tokens(model_id: str) -> frozenset[str]:
    s = (model_id or "").strip().lower()
    return frozenset(m.group(2).lower() for m in _SIBLING_RE.finditer(s))


def allows_synonym_target(
    catalog_id: str,
    synonym: str,
    snapshot_key: str,
    graph: SynonymGraph,
) -> bool:
    cat_mod = gi_ranking.modality_tokens(catalog_id)
    key_mod = gi_ranking.modality_tokens(snapshot_key)
    if cat_mod - key_mod:
        return False
    extra = sibling_tokens(catalog_id) - sibling_tokens(snapshot_key)
    if not extra:
        return True
    return (
        graph.trusted_pair(catalog_id, synonym)
        or graph.trusted_pair(catalog_id, snapshot_key)
        or graph.trusted_pair(synonym, snapshot_key)
    )


def resolve_via_synonyms(
    catalog_id: str,
    known_keys: set[str],
    graph: SynonymGraph,
    match_fn=None,
) -> str | None:
    """Walk synonym component only (caller already tried deterministic_match)."""
    if match_fn is None:
        from refresh_gi_rankings import deterministic_match as match_fn
    best: str | None = None
    for syn_name in graph.component(catalog_id):
        hit = match_fn(syn_name, known_keys)
        if hit is None:
            continue
        if not allows_synonym_target(catalog_id, syn_name, hit, graph):
            continue
        if best is None or len(hit) > len(best) or (len(hit) == len(best) and hit < best):
            best = hit
    return best


def resolve_catalog_aliases(
    catalog_ids: list[str],
    known_keys: set[str],
    graph: SynonymGraph,
    models: set[str],
    match_fn=None,
) -> dict[str, str]:
    if match_fn is None:
        from refresh_gi_rankings import deterministic_match as match_fn
    out: dict[str, str] = {}
    for cid in catalog_ids:
        if match_fn(cid, known_keys) is not None:
            continue  # deterministic path handled by refresh
        hit = resolve_via_synonyms(cid, known_keys, graph, match_fn=match_fn)
        if hit is None or hit not in models:
            continue
        norm = gi_ranking.normalize_model_id(cid) or cid.strip().lower()
        if norm and norm != hit:
            out[norm] = hit
    return out
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_gi_synonyms.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/gi_synonyms.py tests/test_gi_synonyms.py
git commit -m "$(cat <<'EOF'
feat: resolve GI catalog aliases via synonym graph

EOF
)"
```

---

### Task 4: Wire synonym graph into `refresh_gi_rankings.py`

**Files:**
- Modify: `scripts/refresh_gi_rankings.py`
- Modify: `tests/test_refresh_gi_rankings.py`

**Interfaces:**
- Consumes: `gi_synonyms.build_synonym_graph`, `load_plugin_aliases`, `load_json_file`, `fetch_json`, `resolve_catalog_aliases`, default paths/URLs
- Extends: `run_refresh(...)` with optional kwargs:
  - `openrouter: Path | None = None`
  - `litellm: Path | None = None`
  - `llm_aliases: Path | None = None`
  - `offline: bool = False`
  - `openrouter_payload: object | None = None` (tests inject without I/O)
  - `litellm_payload: object | None = None`
  - `fetch_openrouter: Callable | None = None`
  - `fetch_litellm: Callable | None = None`
- Coverage dict may include `via: {"deterministic": int, "synonym": int, "llm": int}`

**Match order in `run_refresh` when `catalog` is set:**

1. Deterministic match → count `deterministic`, maybe add alias if `norm != hit`
2. Remaining → `resolve_catalog_aliases` → count `synonym`, merge aliases
3. Still remaining → `--llm` as today → count `llm`
4. Recompute `matched` / coverage; fail if below floor

**Source loading helper:**

```python
def _load_synonym_sources(
    *,
    offline: bool,
    openrouter: Path | None,
    litellm: Path | None,
    llm_aliases: Path | None,
    openrouter_payload,
    litellm_payload,
    fetch_openrouter,
    fetch_litellm,
) -> tuple[object | None, object | None, dict[str, str]]:
    import gi_synonyms as syn

    plugin_path = llm_aliases or syn.DEFAULT_PLUGIN_ALIASES_PATH
    plugins = syn.load_plugin_aliases(plugin_path)

    or_data = openrouter_payload
    if or_data is None:
        if openrouter is not None:
            or_data = syn.load_json_file(openrouter)
            if or_data is None:
                raise SystemExit(f"OpenRouter file unreadable: {openrouter}")
        elif offline:
            path = syn.DEFAULT_OPENROUTER_PATH
            or_data = syn.load_json_file(path)
            if or_data is None:
                raise SystemExit(f"--offline requires OpenRouter file at {path}")
        else:
            fetch = fetch_openrouter or (lambda: syn.fetch_json(syn.OPENROUTER_URL))
            try:
                or_data = fetch()
            except Exception as e:
                raise SystemExit(f"OpenRouter fetch failed: {e}") from e

    lt_data = litellm_payload
    if lt_data is None:
        if litellm is not None:
            lt_data = syn.load_json_file(litellm)
            if lt_data is None:
                raise SystemExit(f"LiteLLM file unreadable: {litellm}")
        elif offline:
            path = syn.DEFAULT_LITELLM_PATH
            lt_data = syn.load_json_file(path)
            if lt_data is None:
                raise SystemExit(f"--offline requires LiteLLM file at {path}")
        else:
            fetch = fetch_litellm or (lambda: syn.fetch_json(syn.LITELLM_URL))
            try:
                lt_data = fetch()
            except Exception as e:
                raise SystemExit(f"LiteLLM fetch failed: {e}") from e

    return or_data, lt_data, plugins
```

When `catalog is None`, skip synonym loading entirely (no network).

When `catalog` is set but both payloads end up empty/`None` after warnings, build an empty graph and continue (deterministic + llm only).

CLI:

```python
ap.add_argument("--openrouter", type=Path, default=None)
ap.add_argument("--litellm", type=Path, default=None)
ap.add_argument("--llm-aliases", type=Path, default=None)
ap.add_argument("--offline", action="store_true")
```

Pass through to `run_refresh`.

- [ ] **Step 1: Write failing refresh tests**

```python
def test_run_refresh_synonym_before_llm(tmp_path):
    lmsys = tmp_path / "lmsys.json"
    lmsys.write_text(json.dumps([{"id": "cool-model-v2", "score": 1000}]))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(["vendor/cool-model-latest", "unknown-xyz"]))
    out = tmp_path / "out.json"
    or_payload = {"data": [{
        "id": "vendor/cool-model-latest",
        "canonical_slug": "vendor/cool-model-latest",
        "alias_target": {"slug": "vendor/cool-model-v2"},
    }]}

    def boom_llm(unmatched, known):
        raise AssertionError("llm should not be required for synonym hit")

    code = refresh.run_refresh(
        lmsys=lmsys,
        aa=None,
        out=out,
        catalog=catalog,
        use_llm=True,
        llm_propose=boom_llm,
        coverage_floor=0.4,
        offline=True,
        openrouter_payload=or_payload,
        litellm_payload={},  # empty ok
        llm_aliases=tmp_path / "missing-plugins.json",
    )
    assert code == 0
    doc = json.loads(out.read_text())
    assert doc["aliases"].get("cool-model-latest") == "cool-model-v2"
    assert doc["coverage"]["via"]["synonym"] >= 1


def test_run_refresh_offline_missing_openrouter_exits(tmp_path):
    lmsys = tmp_path / "lmsys.json"
    lmsys.write_text(json.dumps([{"id": "gpt-4o", "score": 1}]))
    catalog = tmp_path / "c.json"
    catalog.write_text(json.dumps(["gpt-4o"]))
    out = tmp_path / "out.json"
    with pytest.raises(SystemExit, match="OpenRouter"):
        refresh.run_refresh(
            lmsys=lmsys,
            aa=None,
            out=out,
            catalog=catalog,
            offline=True,
            openrouter=tmp_path / "nope-or.json",
            litellm=tmp_path / "nope-lt.json",
        )
```

For the offline-missing test: create empty LiteLLM file so failure message is specifically OpenRouter, or expect either. Prefer writing a valid empty litellm `{}` and missing OR file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_refresh_gi_rankings.py::test_run_refresh_synonym_before_llm tests/test_refresh_gi_rankings.py::test_run_refresh_offline_missing_openrouter_exits -v`

Expected: FAIL (`run_refresh` unexpected kwargs / missing `via`).

- [ ] **Step 3: Implement wiring in `run_refresh` + `main`**

Update the catalog loop:

```python
via = {"deterministic": 0, "synonym": 0, "llm": 0}
# ... after building models/known_keys ...
if catalog is not None:
    catalog_ids = load_catalog_ids(catalog)
    aliases = {}
    unmatched = []
    for cid in catalog_ids:
        hit = deterministic_match(cid, known_keys)
        if hit is not None:
            via["deterministic"] += 1
            norm = gi_ranking.normalize_model_id(cid) or cid.strip().lower()
            if norm and norm != hit and hit in models:
                aliases[norm] = hit
        else:
            unmatched.append(cid)

    or_data, lt_data, plugins = _load_synonym_sources(...)
    graph = syn.build_synonym_graph(
        openrouter=or_data, litellm=lt_data, plugin_aliases=plugins
    )
    syn_aliases = syn.resolve_catalog_aliases(
        unmatched, known_keys, graph, set(models.keys()),
        match_fn=deterministic_match,
    )
    for sk, tk in syn_aliases.items():
        aliases[sk] = tk
        via["synonym"] += 1
    still = []
    for cid in unmatched:
        norm = gi_ranking.normalize_model_id(cid) or cid.strip().lower()
        if norm in aliases and aliases[norm] in models:
            continue
        if deterministic_match(cid, known_keys):
            continue
        still.append(cid)
    unmatched = still

    if use_llm and unmatched:
        ...
        via["llm"] += len(filtered)  # or recount carefully

    # recount matched across all catalog ids
    matched = 0
    for cid in catalog_ids:
        if deterministic_match(cid, known_keys):
            matched += 1
            continue
        norm = gi_ranking.normalize_model_id(cid) or cid.strip().lower()
        if norm in aliases and aliases[norm] in models:
            matched += 1
    coverage = coverage_summary(matched, len(catalog_ids))
    coverage["via"] = via
```

Preserve existing tests (`test_build_doc_includes_aliases_and_coverage`): pass `openrouter_payload={}` and `litellm_payload={}` and `offline=True` so they do not hit the network. Update that test’s `run_refresh` call accordingly.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_refresh_gi_rankings.py tests/test_gi_synonyms.py tests/test_gi_ranking.py -q`

Expected: PASS (no network).

- [ ] **Step 5: Commit**

```bash
git add scripts/refresh_gi_rankings.py tests/test_refresh_gi_rankings.py
git commit -m "$(cat <<'EOF'
feat: wire synonym graph into GI rankings refresh

EOF
)"
```

---

### Task 5: Document sources in `data/gi_sources/README.md`

**Files:**
- Modify: `data/gi_sources/README.md`

**Interfaces:** none (docs only)

- [ ] **Step 1: Update README** with a new section after Artificial Analysis:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add data/gi_sources/README.md
git commit -m "$(cat <<'EOF'
docs: document GI synonym source fetch and offline paths

EOF
)"
```

---

## Self-review (plan vs spec)

| Spec requirement | Task |
|------------------|------|
| Synonym graph at refresh | 1–3 |
| OpenRouter edges (id, slug, date-strip, alias_target, HF) | 2 |
| LiteLLM chat keys + provider strip | 2 |
| llm plugin alias map file | 1, 2, 5 |
| Resolve via component + longest/lexicographic key | 3 |
| Sibling + modality refuse rules | 3 |
| CLI `--openrouter/--litellm/--llm-aliases/--offline` | 4 |
| Live default + offline files | 4 |
| `--llm` last resort | 4 |
| Coverage via counts optional | 4 |
| Proxy unchanged | (no proxy tasks) |
| README docs | 5 |
| Unit tests, no network | 1–4 |

No TBD placeholders. Types/names consistent: `SynonymGraph`, `build_synonym_graph`, `resolve_catalog_aliases`, `run_refresh(..., offline=..., openrouter_payload=...)`.
