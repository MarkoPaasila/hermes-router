"""Tests for general intelligence ranking (GI)."""
import json

import gi_ranking as gi
import pytest


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("GI_RANKINGS_FILE", str(tmp_path / "gi_rankings.json"))
    monkeypatch.setenv("GI_OVERRIDES_FILE", str(tmp_path / "gi_overrides.json"))
    gi.reset_for_tests()
    yield
    gi.reset_for_tests()


def test_median_normalized_single():
    assert gi.median_normalized([42.0]) == 42.0


def test_median_normalized_two():
    assert gi.median_normalized([10.0, 30.0]) == 20.0


def test_median_normalized_three():
    assert gi.median_normalized([10.0, 20.0, 90.0]) == 20.0


def test_normalize_min_max():
    assert gi.normalize_min_max([0.0, 50.0, 100.0]) == [0.0, 50.0, 100.0]
    assert gi.normalize_min_max([7.0, 7.0]) == [50.0, 50.0]


def test_min_gi_for_complexity():
    assert gi.min_gi_for_complexity(1) == 0.0
    assert gi.min_gi_for_complexity(3) == 40.0
    assert gi.min_gi_for_complexity(5) == 80.0


def test_resolve_default_without_snapshot():
    score, src = gi.resolve_gi("gemini", "totally-unknown-xyz")
    assert score == 0.0
    assert src == "default"


def test_resolve_snapshot_then_override(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"flash-lite": {"gi": 45.0}},
    }))
    gi.load_snapshot(force=True)
    score, src = gi.resolve_gi("gemini", "gemini-2.5-flash-lite")
    assert score == 45.0
    assert src == "snapshot"

    gi.set_override("gemini", "gemini-2.5-flash-lite", 66.0)
    score, src = gi.resolve_gi("gemini", "gemini-2.5-flash-lite")
    assert score == 66.0
    assert src == "override"

    assert gi.clear_override("gemini", "gemini-2.5-flash-lite") is True
    score, src = gi.resolve_gi("gemini", "gemini-2.5-flash-lite")
    assert score == 45.0
    assert src == "snapshot"


def test_set_override_rejects_out_of_range():
    with pytest.raises(ValueError):
        gi.set_override("gemini", "x", 101.0)
    with pytest.raises(ValueError):
        gi.set_override("gemini", "x", -1.0)


def test_override_persists(tmp_path):
    gi.set_override("openai", "gpt-4o", 88.5)
    path = tmp_path / "gi_overrides.json"
    assert path.exists()
    gi.reset_for_tests()
    gi.load_overrides(force=True)
    score, src = gi.resolve_gi("openai", "gpt-4o")
    assert score == 88.5
    assert src == "override"


def test_normalize_model_id_strips_org_and_tags():
    assert gi.normalize_model_id("OpenAI/GPT-4o:free") == "gpt-4o"
    assert gi.normalize_model_id("meta-llama/llama-3.3-70b-instruct") == "llama-3.3-70b-instruct"
    assert gi.normalize_model_id("qwen/qwen3-32b-q4_k_m") == "qwen3-32b"
    assert gi.normalize_model_id("  DeepSeek/DeepSeek-V3  ") == "deepseek-v3"
    assert gi.normalize_model_id("deepseek-v4-flash-free") == "deepseek-v4-flash"
    assert gi.normalize_model_id("openai/gpt-4o_free") == "gpt-4o"
    assert gi.normalize_model_id("google/gemini-2.5-flash-lite:free") == "gemini-2.5-flash-lite"


def test_lite_and_flash_siblings_resolve_distinct(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {
            "gemini-2.5-flash": {"gi": 72.0},
            "gemini-2.5-flash-lite": {"gi": 50.0},
            "deepseek-v4": {"gi": 80.0},
            "deepseek-v4-flash": {"gi": 40.0},
        },
    }))
    gi.load_snapshot(force=True)
    assert gi.resolve_gi("x", "gemini-2.5-flash") == (72.0, "snapshot")
    assert gi.resolve_gi("x", "gemini-2.5-flash-lite") == (50.0, "snapshot")
    assert gi.resolve_gi("x", "google/gemini-2.5-flash-lite:free") == (50.0, "snapshot")
    assert gi.resolve_gi("x", "deepseek-v4") == (80.0, "snapshot")
    assert gi.resolve_gi("x", "deepseek-v4-flash") == (40.0, "snapshot")
    assert gi.resolve_gi("x", "deepseek-v4-flash-free") == (40.0, "snapshot")


def test_base_does_not_inherit_longer_lite_or_flash_sibling(tmp_path):
    """Longer sibling keys must not win when looking up the base id."""
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {
            "gemini-2.5-flash-lite": {"gi": 50.0},
            "deepseek-v4-flash": {"gi": 40.0},
        },
    }))
    gi.load_snapshot(force=True)
    score, src = gi.resolve_gi("x", "gemini-2.5-flash")
    assert src == "default"
    assert score == 0.0
    score, src = gi.resolve_gi("x", "deepseek-v4")
    assert src == "default"
    assert score == 0.0
    assert gi.resolve_gi("x", "gemini-2.5-flash-lite") == (50.0, "snapshot")
    assert gi.resolve_gi("x", "deepseek-v4-flash") == (40.0, "snapshot")


def test_resolve_via_normalized_org_prefix(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-4o": {"gi": 90.0}},
    }))
    gi.load_snapshot(force=True)
    score, src = gi.resolve_gi("openrouter", "openai/gpt-4o:free")
    assert score == 90.0
    assert src == "snapshot"


def test_resolve_via_alias(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"deepseek-v3": {"gi": 72.0}},
        "aliases": {"deepseek-chat-v3": "deepseek-v3"},
    }))
    gi.load_snapshot(force=True)
    score, src = gi.resolve_gi("openrouter", "deepseek/deepseek-chat-v3")
    assert score == 72.0
    assert src == "snapshot"


def test_override_wins_over_alias(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"deepseek-v3": {"gi": 72.0}},
        "aliases": {"deepseek-chat-v3": "deepseek-v3"},
    }))
    gi.load_snapshot(force=True)
    gi.set_override("openrouter", "deepseek/deepseek-chat-v3", 55.0)
    score, src = gi.resolve_gi("openrouter", "deepseek/deepseek-chat-v3")
    assert score == 55.0
    assert src == "override"


def test_bad_alias_ignored(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-4o": {"gi": 90.0}},
        "aliases": {"mystery": "does-not-exist"},
    }))
    gi.load_snapshot(force=True)
    score, src = gi.resolve_gi("x", "mystery")
    assert score == 0.0
    assert src == "default"


def test_short_substring_keys_do_not_match(tmp_path):
    """Keys shorter than MIN_SUBSTRING_KEY_LEN must not false-hit longer ids."""
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"o1": {"gi": 95.0}, "yi": {"gi": 40.0}},
    }))
    gi.load_snapshot(force=True)
    # "o1" is len 2; must not match via substring into unrelated ids
    score, src = gi.resolve_gi("x", "openai/gpt-oss-120b")
    assert src == "default"
    assert score == 0.0
    # Exact short key still works
    score, src = gi.resolve_gi("x", "o1")
    assert score == 95.0
    assert src == "snapshot"
