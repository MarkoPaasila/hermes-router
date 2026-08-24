#!/usr/bin/env python3
"""Build gi_rankings.json from LMSYS + Artificial Analysis source JSON files.

Usage:
  python scripts/refresh_gi_rankings.py \\
    --lmsys path/to/lmsys.json \\
    --aa path/to/artificial_analysis.json \\
    --catalog path/to/catalog.json \\
    --llm \\
    --out gi_rankings.json

Each score input file is a JSON list (or {"models": [...]}) of objects with at least:
  {"id": "model-id", "score": <number>}

Scores are min–max normalized per source into 0–100, then combined with the
median across available sources for each model id.

Optional --catalog lists model ids (or {provider, model} / {id} / {model} objects)
used to compute coverage and build aliases. With --llm, unmatched catalog ids are
sent to an OpenAI-compatible chat endpoint (GI_ALIAS_LLM_*) for alias proposals
validated against known snapshot keys. Exit code 1 if --catalog coverage < 80%.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# Allow importing project modules when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gi_ranking  # noqa: E402

DEFAULT_COVERAGE_FLOOR = 0.80


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


def coverage_summary(matched: int, total: int) -> dict[str, float | int]:
    if total <= 0:
        return {"matched": 0, "total": 0, "pct": 100.0}
    pct = 100.0 * matched / total
    return {"matched": matched, "total": total, "pct": round(pct, 3)}


def load_catalog_ids(path: Path) -> list[str]:
    doc = json.loads(path.read_text())
    items = doc.get("models") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise SystemExit(f"{path}: expected a list or {{'models': [...]}}")
    out: list[str] = []
    for item in items:
        if isinstance(item, str):
            mid = item.strip()
        elif isinstance(item, dict):
            mid = (item.get("model") or item.get("id") or item.get("name") or "").strip()
        else:
            continue
        if mid:
            out.append(mid)
    return out


def deterministic_match(catalog_id: str, known_keys: set[str]) -> str | None:
    """Exact → normalized → longest contained key (min key length)."""
    raw = (catalog_id or "").strip().lower()
    if not raw:
        return None
    if raw in known_keys:
        return raw
    norm = gi_ranking.normalize_model_id(raw)
    if norm in known_keys:
        return norm

    candidates = [raw]
    if norm and norm not in candidates:
        candidates.append(norm)

    best: str | None = None
    for key in known_keys:
        if len(key) < gi_ranking.MIN_SUBSTRING_KEY_LEN:
            continue
        for cand in candidates:
            if gi_ranking.allows_contained_match(key, cand):
                if best is None or len(key) > len(best):
                    best = key
                break
    return best


def filter_llm_proposals(
    proposals: dict[str, str | None],
    known_keys: set[str],
) -> dict[str, str]:
    """Keep only proposals whose target is in known_keys; store under normalized id."""
    out: dict[str, str] = {}
    for src, dst in proposals.items():
        if not dst or not isinstance(dst, str):
            continue
        target = dst.strip().lower()
        if target not in known_keys:
            continue
        sk = gi_ranking.normalize_model_id(src) or (src or "").strip().lower()
        if not sk:
            continue
        out[sk] = target
    return out


def default_llm_propose(unmatched: list[str], known_keys: set[str]) -> dict[str, str | None]:
    """Call OpenAI-compatible chat API for alias proposals."""
    if not unmatched:
        return {}
    base = (os.environ.get("GI_ALIAS_LLM_BASE_URL") or "").rstrip("/")
    key = os.environ.get("GI_ALIAS_LLM_API_KEY") or ""
    model = os.environ.get("GI_ALIAS_LLM_MODEL") or "gpt-4o-mini"
    if not base or not key:
        raise SystemExit(
            "--llm requires GI_ALIAS_LLM_BASE_URL and GI_ALIAS_LLM_API_KEY"
        )

    key_list = sorted(known_keys)
    # Cap prompt size: send unmatched + a truncated key list note
    system = (
        "You map provider model ids to canonical leaderboard keys. "
        "Reply with JSON only: an object mapping each input id to a key from "
        "the provided key list, or null if none fit. Do not invent keys."
    )
    user = json.dumps({
        "catalog_ids": unmatched,
        "known_keys": key_list,
    })
    url = f"{base}/chat/completions"
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        raise SystemExit(f"LLM request failed: {e}") from e

    try:
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
        raise SystemExit(f"LLM response not usable JSON object: {e}") from e

    if not isinstance(parsed, dict):
        raise SystemExit("LLM response must be a JSON object")
    return {str(k): (None if v is None else str(v)) for k, v in parsed.items()}


def build_models_from_sources(
    present: dict[str, dict[str, float]],
) -> dict[str, dict]:
    normalized: dict[str, dict[str, float]] = {}
    for src, raw in present.items():
        ids = list(raw.keys())
        norms = gi_ranking.normalize_min_max([raw[i] for i in ids])
        for mid, n in zip(ids, norms):
            normalized.setdefault(mid, {})[src] = n

    models: dict[str, dict] = {}
    for mid, src_scores in sorted(normalized.items()):
        gi = gi_ranking.median_normalized(list(src_scores.values()))
        models[mid] = {
            "gi": round(gi, 2),
            "sources": {k: round(v, 2) for k, v in src_scores.items()},
        }
    return models


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


def run_refresh(
    *,
    lmsys: Path | None,
    aa: Path | None,
    out: Path,
    catalog: Path | None = None,
    use_llm: bool = False,
    llm_propose: Callable[[list[str], set[str]], dict[str, str | None]] | None = None,
    coverage_floor: float = DEFAULT_COVERAGE_FLOOR,
    openrouter: Path | None = None,
    litellm: Path | None = None,
    llm_aliases: Path | None = None,
    offline: bool = False,
    openrouter_payload: object | None = None,
    litellm_payload: object | None = None,
    fetch_openrouter: Callable[[], object] | None = None,
    fetch_litellm: Callable[[], object] | None = None,
    prior_snapshot: dict | None = None,
    prior_snapshot_path: Path | None = None,
    note: str | None = None,
) -> int:
    """Build snapshot. Returns 0 on success, 1 if catalog coverage below floor."""
    sources_raw = {
        "lmsys": _load_scores(lmsys),
        "artificial_analysis": _load_scores(aa),
    }
    present = {k: v for k, v in sources_raw.items() if v}
    if not present:
        raise SystemExit("Provide at least one of --lmsys / --aa with score entries")

    if prior_snapshot is None and prior_snapshot_path and prior_snapshot_path.exists():
        prior_snapshot = json.loads(prior_snapshot_path.read_text())

    models = build_models_from_sources(present)
    models = apply_seed_overlay(models, prior_snapshot)
    known_keys = set(models.keys())
    # Also allow matching on normalized forms of keys
    for k in list(known_keys):
        nk = gi_ranking.normalize_model_id(k)
        if nk and nk not in known_keys and nk in models:
            known_keys.add(nk)

    aliases: dict[str, str] = {}
    coverage = None
    exit_code = 0

    if catalog is not None:
        import gi_synonyms as syn

        catalog_ids = load_catalog_ids(catalog)
        via = {"deterministic": 0, "synonym": 0, "llm": 0}
        unmatched: list[str] = []
        for cid in catalog_ids:
            hit = deterministic_match(cid, known_keys)
            if hit is not None:
                via["deterministic"] += 1
                norm = gi_ranking.normalize_model_id(cid) or cid.strip().lower()
                if norm and norm != hit and hit in models:
                    aliases[norm] = hit
            else:
                unmatched.append(cid)

        or_data, lt_data, plugins = _load_synonym_sources(
            offline=offline,
            openrouter=openrouter,
            litellm=litellm,
            llm_aliases=llm_aliases,
            openrouter_payload=openrouter_payload,
            litellm_payload=litellm_payload,
            fetch_openrouter=fetch_openrouter,
            fetch_litellm=fetch_litellm,
        )
        graph = syn.build_synonym_graph(
            openrouter=or_data,
            litellm=lt_data,
            plugin_aliases=plugins,
        )
        syn_aliases = syn.resolve_catalog_aliases(
            unmatched,
            known_keys,
            graph,
            set(models.keys()),
            match_fn=deterministic_match,
        )
        for sk, tk in syn_aliases.items():
            aliases[sk] = tk
            via["synonym"] += 1

        still: list[str] = []
        for cid in unmatched:
            norm = gi_ranking.normalize_model_id(cid) or cid.strip().lower()
            if norm in aliases and aliases[norm] in models:
                continue
            if deterministic_match(cid, known_keys):
                continue
            still.append(cid)
        unmatched = still

        if use_llm and unmatched:
            propose = llm_propose or default_llm_propose
            proposals = propose(unmatched, set(models.keys()))
            filtered = filter_llm_proposals(proposals, set(models.keys()))
            unmatched_norms = {
                gi_ranking.normalize_model_id(cid) or cid.strip().lower()
                for cid in unmatched
            }
            for sk, tk in filtered.items():
                aliases[sk] = tk
                if sk in unmatched_norms:
                    via["llm"] += 1

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
        pct_frac = (coverage["pct"] / 100.0) if coverage["total"] else 1.0
        if pct_frac < coverage_floor:
            exit_code = 1
            print(
                f"Coverage {coverage['pct']}% "
                f"({coverage['matched']}/{coverage['total']}) "
                f"below floor {coverage_floor * 100:.0f}%",
                file=sys.stderr,
            )

    doc: dict = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sorted(
            set(present.keys())
            | (
                {"seed"}
                if any(
                    "seed" in (m.get("sources") or {})
                    for m in models.values()
                )
                else set()
            )
        ),
        "aliases": dict(sorted(aliases.items())),
        "models": models,
    }
    if note:
        doc["note"] = note
    if coverage is not None:
        doc["coverage"] = coverage

    out.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"Wrote {len(models)} models, {len(aliases)} aliases to {out}")
    if coverage is not None:
        print(f"Catalog coverage: {coverage['matched']}/{coverage['total']} ({coverage['pct']}%)")
    return exit_code


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lmsys", type=Path, default=None, help="LMSYS Arena scores JSON")
    ap.add_argument("--aa", type=Path, default=None, help="Artificial Analysis scores JSON")
    ap.add_argument("--catalog", type=Path, default=None, help="Runtime catalog model ids JSON")
    ap.add_argument("--llm", action="store_true", help="Propose aliases for unmatched via LLM")
    ap.add_argument("--openrouter", type=Path, default=None)
    ap.add_argument("--litellm", type=Path, default=None)
    ap.add_argument("--llm-aliases", type=Path, default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument(
        "--prior",
        type=Path,
        default=None,
        help="Prior gi_rankings.json for seed-only retention (default: gi_rankings.json if present)",
    )
    ap.add_argument("--note", type=str, default=None, help="Optional note stored in output JSON")
    ap.add_argument("--out", type=Path, default=ROOT / "gi_rankings.json")
    ap.add_argument(
        "--coverage-floor",
        type=float,
        default=DEFAULT_COVERAGE_FLOOR,
        help="Fail if catalog coverage fraction is below this (default 0.8)",
    )
    args = ap.parse_args(argv)

    prior_path = args.prior
    if prior_path is None:
        default_prior = ROOT / "gi_rankings.json"
        prior_path = default_prior if default_prior.exists() else None

    code = run_refresh(
        lmsys=args.lmsys,
        aa=args.aa,
        out=args.out,
        catalog=args.catalog,
        use_llm=args.llm,
        coverage_floor=args.coverage_floor,
        openrouter=args.openrouter,
        litellm=args.litellm,
        llm_aliases=args.llm_aliases,
        offline=args.offline,
        prior_snapshot_path=prior_path,
        note=args.note,
    )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
