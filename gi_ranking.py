"""General intelligence ranking (GI): snapshot defaults, overrides, thresholds.

Replaces the old 1–5 Capability score. Scale is 0–100 (higher = stronger).
Resolution: dashboard override → snapshot → 0 (bottom of pack).

Persistence:
  • Snapshot scores (`gi_rankings.json`) are read-only defaults. They are re-loaded
    from disk on every process start/restart (and when the file mtime changes).
    The proxy never writes snapshot scores into router_state.
  • Only manual overrides (`gi_overrides.json`) persist operator-set scores.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

COMPLEXITY_MIN_GI: dict[int, float] = {
    1: 0.0,
    2: 20.0,
    3: 40.0,
    4: 60.0,
    5: 80.0,
}

# Substring snapshot keys shorter than this are exact/alias-only (no fuzzy hit).
MIN_SUBSTRING_KEY_LEN = 4

# Specialty modality tokens: cand with these must not inherit a chat key that lacks them.
_MODALITY_TOKENS = frozenset({
    "image",
    "veo",
    "live",
    "omni",
    "translate",
    "computer-use",
})

_QUANT_SUFFIX_RE = re.compile(
    r"[-_]("
    r"q[2-8](?:_[0-9k_m]+)?|"
    r"gguf|awq|gptq|fp8|int[48]|bf16|fp16"
    r")$",
    re.IGNORECASE,
)

_FREE_SUFFIX_RE = re.compile(r"[-_]free$", re.IGNORECASE)

_lock = threading.RLock()
_snapshot: dict[str, float] = {}  # lowercased model key → gi
_aliases: dict[str, str] = {}  # normalized catalog id → canonical snapshot key
_overrides: dict[str, float] = {}  # "provider|model" → gi
_snapshot_loaded = False
_overrides_loaded = False
_snapshot_mtime: float | None = None
_overrides_mtime: float | None = None


def normalize_model_id(model: str) -> str:
    """Lowercase, strip org/, :tag, trailing -free, and common quant suffixes."""
    s = (model or "").strip().lower()
    if not s:
        return ""
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if ":" in s:
        s = s.split(":", 1)[0]
    s = _FREE_SUFFIX_RE.sub("", s)
    # Drop trailing quant tags repeatedly (e.g. -q4_k_m)
    while True:
        m = _QUANT_SUFFIX_RE.search(s)
        if not m:
            break
        s = s[: m.start()]
    return s.strip("-_")


def modality_tokens(model_id: str) -> frozenset[str]:
    """Specialty modality tokens present as hyphen/underscore-delimited segments."""
    s = (model_id or "").strip().lower()
    if not s:
        return frozenset()
    found: set[str] = set()
    for tok in _MODALITY_TOKENS:
        if re.search(rf"(^|[-_]){re.escape(tok)}([-_]|$)", s):
            found.add(tok)
    return frozenset(found)


def allows_contained_match(key: str, cand: str) -> bool:
    """True if key is a substring of cand and cand adds no extra modality tokens."""
    if not key or key not in cand:
        return False
    extra = modality_tokens(cand) - modality_tokens(key)
    return not extra


def rankings_path() -> Path:
    return Path(os.environ.get("GI_RANKINGS_FILE", "./gi_rankings.json"))


def overrides_path() -> Path:
    return Path(os.environ.get("GI_OVERRIDES_FILE", "./gi_overrides.json"))


def override_key(provider: str, model: str) -> str:
    return f"{(provider or '').strip()}|{(model or '').strip()}"


def median_normalized(scores: list[float]) -> float:
    """Median of already-normalized 0–100 scores. Single value → that value."""
    if not scores:
        raise ValueError("scores must be non-empty")
    vals = [float(s) for s in scores]
    return float(statistics.median(vals))


def normalize_min_max(raw_scores: list[float]) -> list[float]:
    """Min–max normalize a list of raw scores into 0–100. Constant list → all 50."""
    if not raw_scores:
        return []
    lo, hi = min(raw_scores), max(raw_scores)
    if hi <= lo:
        return [50.0] * len(raw_scores)
    return [100.0 * (x - lo) / (hi - lo) for x in raw_scores]


def min_gi_for_complexity(complexity: int) -> float:
    c = int(complexity)
    if c in COMPLEXITY_MIN_GI:
        return COMPLEXITY_MIN_GI[c]
    if c <= 1:
        return COMPLEXITY_MIN_GI[1]
    return COMPLEXITY_MIN_GI[5]


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime if path.exists() else None
    except OSError:
        return None


def _score_for_key(key: str) -> float | None:
    """Resolve a snapshot key or alias target to a GI score."""
    if key in _snapshot:
        return _snapshot[key]
    target = _aliases.get(key)
    if target and target in _snapshot:
        return _snapshot[target]
    return None


def _match_snapshot(model: str) -> float | None:
    """Exact → normalized → alias → longest contained key (min key length)."""
    raw = (model or "").strip().lower()
    if not raw:
        return None

    hit = _score_for_key(raw)
    if hit is not None:
        return hit

    norm = normalize_model_id(raw)
    if norm:
        hit = _score_for_key(norm)
        if hit is not None:
            return hit
        if norm in _aliases:
            target = _aliases[norm]
            if target in _snapshot:
                return _snapshot[target]

    candidates = [raw]
    if norm and norm not in candidates:
        candidates.append(norm)

    best_key = None
    for key in _snapshot:
        if len(key) < MIN_SUBSTRING_KEY_LEN:
            continue
        for cand in candidates:
            if allows_contained_match(key, cand):
                if best_key is None or len(key) > len(best_key):
                    best_key = key
                break
    if best_key is None:
        return None
    return _snapshot[best_key]


def load_snapshot(path: Path | None = None, *, force: bool = False) -> None:
    global _snapshot, _aliases, _snapshot_loaded, _snapshot_mtime
    p = path or rankings_path()
    mtime = _file_mtime(p)
    with _lock:
        if _snapshot_loaded and not force and mtime is not None and mtime == _snapshot_mtime:
            return
        if _snapshot_loaded and not force and mtime is None and _snapshot_mtime is None:
            return
        _snapshot = {}
        _aliases = {}
        if not p.exists():
            log.warning("[gi] snapshot missing at %s — all models default to 0 unless overridden", p)
            _snapshot_mtime = None
            _snapshot_loaded = True
            return
        try:
            doc = json.loads(p.read_text())
            models = doc.get("models") or {}
            for mid, entry in models.items():
                if not isinstance(mid, str):
                    continue
                gi = None
                if isinstance(entry, (int, float)):
                    gi = float(entry)
                elif isinstance(entry, dict) and "gi" in entry:
                    try:
                        gi = float(entry["gi"])
                    except (TypeError, ValueError):
                        log.warning("[gi] skipping bad snapshot entry for %s", mid)
                        continue
                if gi is None:
                    continue
                if not (0.0 <= gi <= 100.0):
                    log.warning("[gi] skipping out-of-range snapshot gi for %s: %s", mid, gi)
                    continue
                _snapshot[mid.lower()] = gi

            raw_aliases = doc.get("aliases") or {}
            if isinstance(raw_aliases, dict):
                for src, dst in raw_aliases.items():
                    if not isinstance(src, str) or not isinstance(dst, str):
                        continue
                    sk = normalize_model_id(src) or src.strip().lower()
                    tk = dst.strip().lower()
                    if not sk or not tk:
                        continue
                    if tk not in _snapshot:
                        log.warning("[gi] skipping alias %s → %s (target missing)", sk, tk)
                        continue
                    _aliases[sk] = tk

            log.info(
                "[gi] loaded %d snapshot scores, %d aliases from %s",
                len(_snapshot),
                len(_aliases),
                p.resolve() if p.exists() else p,
            )
        except Exception as e:
            log.warning("[gi] could not read snapshot %s: %s", p, e)
            _snapshot = {}
            _aliases = {}
        _snapshot_mtime = _file_mtime(p)
        _snapshot_loaded = True


def load_overrides(path: Path | None = None, *, force: bool = False) -> None:
    global _overrides, _overrides_loaded, _overrides_mtime
    p = path or overrides_path()
    mtime = _file_mtime(p)
    with _lock:
        if _overrides_loaded and not force and mtime is not None and mtime == _overrides_mtime:
            return
        if _overrides_loaded and not force and mtime is None and _overrides_mtime is None:
            return
        _overrides = {}
        if not p.exists():
            _overrides_mtime = None
            _overrides_loaded = True
            return
        try:
            doc = json.loads(p.read_text())
            raw = doc.get("overrides") or {}
            for k, entry in raw.items():
                if not isinstance(k, str) or "|" not in k:
                    continue
                try:
                    if isinstance(entry, (int, float)):
                        gi = float(entry)
                    elif isinstance(entry, dict):
                        gi = float(entry["gi"])
                    else:
                        continue
                except (KeyError, TypeError, ValueError):
                    log.warning("[gi] skipping bad override entry %s", k)
                    continue
                if not (0.0 <= gi <= 100.0):
                    log.warning("[gi] skipping out-of-range override %s: %s", k, gi)
                    continue
                _overrides[k] = gi
            log.info("[gi] loaded %d overrides from %s", len(_overrides), p)
        except Exception as e:
            log.warning("[gi] could not read overrides %s: %s", p, e)
            _overrides = {}
        _overrides_mtime = _file_mtime(p)
        _overrides_loaded = True


def _ensure_loaded() -> None:
    load_snapshot()
    load_overrides()


def reload_scores() -> None:
    """Force re-read snapshot + overrides from disk (call on process start)."""
    load_snapshot(force=True)
    load_overrides(force=True)


def resolve_gi(provider: str, model: str) -> tuple[float, str]:
    """Return (gi, source) where source is override|snapshot|default."""
    _ensure_loaded()
    with _lock:
        ok = override_key(provider, model)
        if ok in _overrides:
            return _overrides[ok], "override"
        hit = _match_snapshot(model)
        if hit is not None:
            return hit, "snapshot"
        return 0.0, "default"


def set_override(provider: str, model: str, gi: float, path: Path | None = None) -> float:
    """Set a live override and persist. Raises ValueError if gi out of range."""
    g = float(gi)
    if not (0.0 <= g <= 100.0):
        raise ValueError("gi must be between 0 and 100")
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider or not model:
        raise ValueError("provider and model are required")
    _ensure_loaded()
    key = override_key(provider, model)
    with _lock:
        _overrides[key] = g
        _persist_overrides(path)
        global _overrides_mtime
        _overrides_mtime = _file_mtime(path or overrides_path())
    return g


def clear_override(provider: str, model: str, path: Path | None = None) -> bool:
    """Remove override if present. Returns True if an entry was removed."""
    _ensure_loaded()
    key = override_key(provider, model)
    with _lock:
        existed = key in _overrides
        if existed:
            del _overrides[key]
            _persist_overrides(path)
            global _overrides_mtime
            _overrides_mtime = _file_mtime(path or overrides_path())
        return existed


def _persist_overrides(path: Path | None = None) -> None:
    p = path or overrides_path()
    doc = {
        "overrides": {
            k: {"gi": v, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            for k, v in sorted(_overrides.items())
        }
    }
    p.write_text(json.dumps(doc, indent=2) + "\n")


def reset_for_tests() -> None:
    """Clear in-memory state (tests only)."""
    global _snapshot, _aliases, _overrides, _snapshot_loaded, _overrides_loaded
    global _snapshot_mtime, _overrides_mtime
    with _lock:
        _snapshot = {}
        _aliases = {}
        _overrides = {}
        _snapshot_loaded = False
        _overrides_loaded = False
        _snapshot_mtime = None
        _overrides_mtime = None
