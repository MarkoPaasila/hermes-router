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
