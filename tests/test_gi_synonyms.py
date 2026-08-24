from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import gi_synonyms as syn  # noqa: E402
import refresh_gi_rankings as refresh  # noqa: E402


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
    p.write_text(json.dumps({"aliases": {"flash": "gemini-2.5-flash", "GPT-4o": "openai/gpt-4o"}}))
    got = syn.load_plugin_aliases(p)
    assert got["flash"] == "gemini-2.5-flash"
    assert got["gpt-4o"] == "openai/gpt-4o"


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
    assert "text-embedding-3-small" not in g._parent


def test_add_plugin_alias_edges():
    g = syn.SynonymGraph()
    syn.add_plugin_alias_edges(g, {"flash": "gemini-2.5-flash"})
    assert "gemini-2.5-flash" in g.component("flash")


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


def test_resolve_via_synonyms_alias_target_cannot_drop_mini_on_snapshot_match():
    g = syn.SynonymGraph()
    syn.add_openrouter_edges(g, {"data": [{
        "id": "~openai/gpt-mini-latest",
        "alias_target": {"slug": "openai/gpt-5.4-mini"},
    }]})

    assert syn.resolve_via_synonyms(
        "~openai/gpt-mini-latest",
        {"gpt-5.4"},
        g,
        match_fn=refresh.deterministic_match,
    ) is None


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
