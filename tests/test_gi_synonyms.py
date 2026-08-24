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
