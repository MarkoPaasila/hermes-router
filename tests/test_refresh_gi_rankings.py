"""Tests for scripts/refresh_gi_rankings.py coverage & alias helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_gi_rankings as refresh  # noqa: E402


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


def test_coverage_pct():
    assert refresh.coverage_summary(80, 100) == {"matched": 80, "total": 100, "pct": 80.0}
    assert refresh.coverage_summary(0, 0) == {"matched": 0, "total": 0, "pct": 100.0}
    assert refresh.coverage_summary(1, 3)["pct"] == pytest.approx(33.333, rel=1e-3)


def test_load_catalog_ids(tmp_path):
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps([
        "openai/gpt-4o",
        {"model": "deepseek/deepseek-chat-v3"},
        {"provider": "groq", "model": "llama-3.3-70b"},
        {"id": "qwen/qwen3-32b"},
    ]))
    ids = refresh.load_catalog_ids(p)
    assert ids == [
        "openai/gpt-4o",
        "deepseek/deepseek-chat-v3",
        "llama-3.3-70b",
        "qwen/qwen3-32b",
    ]


def test_deterministic_match_exact_and_normalized():
    keys = {"gpt-4o", "deepseek-v3", "llama-3.3-70b"}
    assert refresh.deterministic_match("gpt-4o", keys) == "gpt-4o"
    assert refresh.deterministic_match("openai/gpt-4o:free", keys) == "gpt-4o"
    assert refresh.deterministic_match("meta-llama/llama-3.3-70b-instruct", keys) == "llama-3.3-70b"
    assert refresh.deterministic_match("totally-unknown", keys) is None


def test_deterministic_match_does_not_conflate_lite_or_flash():
    keys = {
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "deepseek-v4",
        "deepseek-v4-flash",
    }
    assert refresh.deterministic_match("gemini-2.5-flash", keys) == "gemini-2.5-flash"
    assert refresh.deterministic_match("gemini-2.5-flash-lite", keys) == "gemini-2.5-flash-lite"
    assert refresh.deterministic_match("google/gemini-2.5-flash-lite:free", keys) == "gemini-2.5-flash-lite"
    assert refresh.deterministic_match("deepseek-v4", keys) == "deepseek-v4"
    assert refresh.deterministic_match("deepseek-v4-flash-free", keys) == "deepseek-v4-flash"
    # Base must not map to a longer sibling when base is absent
    assert refresh.deterministic_match("gemini-2.5-flash", {"gemini-2.5-flash-lite"}) is None
    assert refresh.deterministic_match("deepseek-v4", {"deepseek-v4-flash"}) is None


def test_deterministic_match_skips_modality_inheritance():
    keys = {"gemini-3-pro", "gemini-2.5-flash"}
    assert refresh.deterministic_match("gemini-3-pro-image", keys) is None
    assert refresh.deterministic_match("gemini-2.5-computer-use-preview", keys) is None
    assert refresh.deterministic_match("gemini-3-pro-preview", keys) == "gemini-3-pro"


def test_filter_llm_proposals_rejects_unknown_keys():
    known = {"deepseek-v3", "gpt-4o"}
    proposals = {
        "deepseek/deepseek-chat-v3": "deepseek-v3",
        "x-ai/grok-3": "grok-3",  # not in known
        "noise": None,
        "openai/gpt-4o-mini": "gpt-4o",
    }
    got = refresh.filter_llm_proposals(proposals, known)
    assert got == {
        "deepseek-chat-v3": "deepseek-v3",
        "gpt-4o-mini": "gpt-4o",
    }


def test_build_doc_includes_aliases_and_coverage(tmp_path):
    lmsys = tmp_path / "lmsys.json"
    lmsys.write_text(json.dumps([
        {"id": "gpt-4o", "score": 1200},
        {"id": "deepseek-v3", "score": 1100},
    ]))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([
        "openai/gpt-4o:free",
        "deepseek/deepseek-chat-v3",
        "unknown-model-xyz",
    ]))
    out = tmp_path / "out.json"

    # Without LLM, deepseek-chat-v3 may not match deepseek-v3 deterministically
    # (different strings). Inject alias via mock LLM.
    def fake_llm(unmatched, known_keys):
        return {"deepseek/deepseek-chat-v3": "deepseek-v3", "bogus": "not-a-key"}

    code = refresh.run_refresh(
        lmsys=lmsys,
        aa=None,
        out=out,
        catalog=catalog,
        use_llm=True,
        llm_propose=fake_llm,
        coverage_floor=0.5,  # 2/3 ≈ 66% would fail at 80%; use 50% for this unit test
        offline=True,
        openrouter_payload={},
        litellm_payload={},
    )
    assert code == 0
    doc = json.loads(out.read_text())
    assert "gpt-4o" in doc["models"]
    assert doc["aliases"].get("deepseek-chat-v3") == "deepseek-v3"
    assert "bogus" not in doc["aliases"]
    assert doc["coverage"]["matched"] == 2
    assert doc["coverage"]["total"] == 3


def test_run_refresh_fails_below_coverage_floor(tmp_path):
    lmsys = tmp_path / "lmsys.json"
    lmsys.write_text(json.dumps([{"id": "gpt-4o", "score": 100}]))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(["gpt-4o", "a", "b", "c", "d"]))
    out = tmp_path / "out.json"
    code = refresh.run_refresh(
        lmsys=lmsys,
        aa=None,
        out=out,
        catalog=catalog,
        use_llm=False,
        coverage_floor=0.8,
        offline=True,
        openrouter_payload={},
        litellm_payload={},
    )
    assert code == 1
    # Still writes the file for inspection
    assert out.exists()


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

    def fake_llm(unmatched, known):
        assert unmatched == ["unknown-xyz"]
        return {}

    code = refresh.run_refresh(
        lmsys=lmsys,
        aa=None,
        out=out,
        catalog=catalog,
        use_llm=True,
        llm_propose=fake_llm,
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
    litellm = tmp_path / "litellm.json"
    litellm.write_text("{}")
    out = tmp_path / "out.json"
    with pytest.raises(SystemExit, match="OpenRouter"):
        refresh.run_refresh(
            lmsys=lmsys,
            aa=None,
            out=out,
            catalog=catalog,
            offline=True,
            openrouter=tmp_path / "nope-or.json",
            litellm=litellm,
        )
