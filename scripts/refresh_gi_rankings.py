#!/usr/bin/env python3
"""Build gi_rankings.json from LMSYS + Artificial Analysis source JSON files.

Usage:
  python scripts/refresh_gi_rankings.py \\
    --lmsys path/to/lmsys.json \\
    --aa path/to/artificial_analysis.json \\
    --out gi_rankings.json

Each input file is a JSON list (or {"models": [...]}) of objects with at least:
  {"id": "model-id", "score": <number>}

Scores are min–max normalized per source into 0–100, then combined with the
median across available sources for each model id.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow importing project modules when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gi_ranking  # noqa: E402


def _load_scores(path: Path | None) -> dict[str, float]:
    if path is None or not path.exists():
        return {}
    doc = json.loads(path.read_text())
    items = doc.get("models") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise SystemExit(f"{path}: expected a list or {{'models': [...]}}")
    out: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = (item.get("id") or item.get("model") or item.get("name") or "").strip()
        if not mid:
            continue
        try:
            score = float(item.get("score", item.get("elo", item.get("rating"))))
        except (TypeError, ValueError):
            continue
        out[mid.lower()] = score
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lmsys", type=Path, default=None, help="LMSYS Arena scores JSON")
    ap.add_argument("--aa", type=Path, default=None, help="Artificial Analysis scores JSON")
    ap.add_argument("--out", type=Path, default=ROOT / "gi_rankings.json")
    args = ap.parse_args()

    sources_raw = {
        "lmsys": _load_scores(args.lmsys),
        "artificial_analysis": _load_scores(args.aa),
    }
    present = {k: v for k, v in sources_raw.items() if v}
    if not present:
        raise SystemExit("Provide at least one of --lmsys / --aa with score entries")

    # Normalize each source independently
    normalized: dict[str, dict[str, float]] = {}
    for src, raw in present.items():
        ids = list(raw.keys())
        norms = gi_ranking.normalize_min_max([raw[i] for i in ids])
        for mid, n in zip(ids, norms):
            normalized.setdefault(mid, {})[src] = n

    models = {}
    for mid, src_scores in sorted(normalized.items()):
        gi = gi_ranking.median_normalized(list(src_scores.values()))
        models[mid] = {"gi": round(gi, 2), "sources": {k: round(v, 2) for k, v in src_scores.items()}}

    doc = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sorted(present.keys()),
        "models": models,
    }
    args.out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Wrote {len(models)} models to {args.out}")


if __name__ == "__main__":
    main()
