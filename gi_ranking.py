"""General intelligence ranking (GI): snapshot defaults, overrides, thresholds.

Replaces the old 1–5 Capability score. Scale is 0–100 (higher = stronger).
Resolution: dashboard override → snapshot → 0 (bottom of pack).
"""
from __future__ import annotations

import json
import logging
import os
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

_lock = threading.RLock()
_snapshot: dict[str, float] = {}  # lowercased model key → gi
_overrides: dict[str, float] = {}  # "provider|model" → gi
_snapshot_loaded = False
_overrides_loaded = False


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


def _match_snapshot(model: str) -> float | None:
    """Longest-substring match against snapshot keys (lowercased)."""
    mn = (model or "").lower()
    if not mn:
        return None
    if mn in _snapshot:
        return _snapshot[mn]
    best_key = None
    for key in _snapshot:
        if key in mn or mn in key:
            if best_key is None or len(key) > len(best_key):
                best_key = key
    if best_key is None:
        return None
    return _snapshot[best_key]


def load_snapshot(path: Path | None = None, *, force: bool = False) -> None:
    global _snapshot, _snapshot_loaded
    with _lock:
        if _snapshot_loaded and not force:
            return
        p = path or rankings_path()
        _snapshot = {}
        if not p.exists():
            log.warning("[gi] snapshot missing at %s — all models default to 0 unless overridden", p)
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
            log.info("[gi] loaded %d snapshot scores from %s", len(_snapshot), p)
        except Exception as e:
            log.warning("[gi] could not read snapshot %s: %s", p, e)
            _snapshot = {}
        _snapshot_loaded = True


def load_overrides(path: Path | None = None, *, force: bool = False) -> None:
    global _overrides, _overrides_loaded
    with _lock:
        if _overrides_loaded and not force:
            return
        p = path or overrides_path()
        _overrides = {}
        if not p.exists():
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
        _overrides_loaded = True


def _ensure_loaded() -> None:
    load_snapshot()
    load_overrides()


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
    global _snapshot, _overrides, _snapshot_loaded, _overrides_loaded
    with _lock:
        _snapshot = {}
        _overrides = {}
        _snapshot_loaded = False
        _overrides_loaded = False
