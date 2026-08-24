"""Synonym graph for GI catalog↔snapshot matching (maintainer refresh only)."""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
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
    # A trusted catalog↔synonym edge only proves spelling equivalence; it cannot
    # license dropping a size/tier token on the way to snapshot_key.
    if sibling_tokens(synonym) - sibling_tokens(snapshot_key):
        return graph.trusted_pair(synonym, snapshot_key) or graph.trusted_pair(
            catalog_id, snapshot_key
        )
    return graph.trusted_pair(catalog_id, synonym) or graph.trusted_pair(
        synonym, snapshot_key
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
