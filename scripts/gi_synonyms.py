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
