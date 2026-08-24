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


def test_min_gi_for_complexity_after_recompute():
    # scored = [20,40,60,80,100] → min/p20/p50/p80 = 20/20/60/80
    gi.recompute_complexity_thresholds([0.0, 20.0, 40.0, 60.0, 80.0, 100.0])
    assert gi.min_gi_for_complexity(1) == 0.0
    assert gi.min_gi_for_complexity(3) == 20.0
    assert gi.min_gi_for_complexity(5) == 80.0


def test_recompute_empty_all_zero():
    m = gi.recompute_complexity_thresholds([])
    assert m == {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}
    assert gi.min_gi_for_complexity(1) == 0.0
    assert gi.min_gi_for_complexity(5) == 0.0


def test_recompute_all_zeros_all_zero():
    m = gi.recompute_complexity_thresholds([0.0, 0.0, 0.0])
    assert m == {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0}


def test_recompute_scored_ladder_excludes_zeros():
    scores = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0]
    m = gi.recompute_complexity_thresholds(scores)
    assert m[1] == 0.0
    assert m[2] == 20.0  # min(scored)
    assert m[3] == 20.0  # p20 of 5 scored
    assert m[4] == 60.0  # p50
    assert m[5] == 80.0  # p80


def test_recompute_c2_is_min_scored():
    m = gi.recompute_complexity_thresholds([0.0, 0.0, 15.0, 50.0, 90.0])
    assert m[2] == 15.0
    assert m[3] == 15.0  # p20 of [15,50,90]
    assert m[4] == 50.0
    assert m[5] == 90.0  # p80


def test_recompute_tiny_n_bars_may_coincide():
    m = gi.recompute_complexity_thresholds([42.0])
    assert m[1] == 0.0
    assert m[2] == m[3] == m[4] == m[5] == 42.0


def test_complexity_1_always_zero_even_if_scores_high():
    m = gi.recompute_complexity_thresholds([90.0, 95.0, 99.0])
    assert m[1] == 0.0


def test_mark_dirty_and_need_refresh():
    gi.recompute_complexity_thresholds([0.0, 50.0, 100.0])
    assert gi.complexity_thresholds_need_refresh() is False
    gi.mark_complexity_thresholds_dirty()
    assert gi.complexity_thresholds_need_refresh() is True


def test_set_override_marks_thresholds_dirty():
    gi.recompute_complexity_thresholds([0.0, 50.0, 100.0])
    assert gi.complexity_thresholds_need_refresh() is False
    gi.set_override("gemini", "x", 66.0)
    assert gi.complexity_thresholds_need_refresh() is True


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


def test_gpt_oss_20b_does_not_inherit_120b(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {
            "gpt-oss-120b": {"gi": 50.0},
            "gpt-oss-20b": {"gi": 42.0},
        },
    }))
    gi.load_snapshot(force=True)
    assert gi.resolve_gi("groq", "openai/gpt-oss-20b") == (42.0, "snapshot")
    assert gi.resolve_gi("openrouter", "openai/gpt-oss-20b:free") == (42.0, "snapshot")
    assert gi.resolve_gi("groq", "openai/gpt-oss-120b") == (50.0, "snapshot")
    # Without a 20b entry, must not inherit 120b via substring
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-oss-120b": {"gi": 50.0}},
    }))
    gi.load_snapshot(force=True)
    score, src = gi.resolve_gi("groq", "openai/gpt-oss-20b")
    assert src == "default"
    assert score == 0.0


def test_modality_sku_does_not_inherit_chat_gi(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {
            "gemini-3-pro": {"gi": 75.0},
            "gemini-2.5-flash": {"gi": 72.0},
        },
    }))
    gi.load_snapshot(force=True)
    for mid in (
        "gemini-3-pro-image",
        "gemini-3-pro-image-preview",
        "gemini-2.5-computer-use-preview-10-2025",
        "gemini-3.1-flash-live-preview",
        "gemini-omni-flash-preview",
        "gemini-3.5-live-translate-preview",
        "veo-3.1-generate-preview",
    ):
        score, src = gi.resolve_gi("gemini", mid)
        assert src == "default", mid
        assert score == 0.0, mid
    # Plain chat ids still match
    assert gi.resolve_gi("gemini", "gemini-3-pro") == (75.0, "snapshot")
    assert gi.resolve_gi("gemini", "gemini-3-pro-preview") == (75.0, "snapshot")


def test_alias_maps_latest_and_near_miss_ids(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {
            "gemini-3.1-flash-lite-preview": {"gi": 32.0},
            "gemini-3.5-flash": {"gi": 76.0},
            "gemini-3.5-flash-lite": {"gi": 68.0},
            "gemini-3.1-pro-preview": {"gi": 75.0},
            "laguna-xs.2": {"gi": 40.0},
        },
        "aliases": {
            "gemini-3.1-flash-lite": "gemini-3.1-flash-lite-preview",
            "gemini-flash-latest": "gemini-3.5-flash",
            "gemini-flash-lite-latest": "gemini-3.5-flash-lite",
            "gemini-pro-latest": "gemini-3.1-pro-preview",
            "laguna-xs-2.1": "laguna-xs.2",
        },
    }))
    gi.load_snapshot(force=True)
    assert gi.resolve_gi("gemini", "gemini-3.1-flash-lite") == (32.0, "snapshot")
    assert gi.resolve_gi("gemini", "gemini-flash-latest") == (76.0, "snapshot")
    assert gi.resolve_gi("gemini", "gemini-flash-lite-latest") == (68.0, "snapshot")
    assert gi.resolve_gi("gemini", "gemini-pro-latest") == (75.0, "snapshot")
    assert gi.resolve_gi("openrouter", "poolside/laguna-xs-2.1:free") == (40.0, "snapshot")


def test_snapshot_reloads_when_file_mtime_changes(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-4o": {"gi": 50.0}},
    }))
    gi.load_snapshot(force=True)
    assert gi.resolve_gi("x", "gpt-4o") == (50.0, "snapshot")

    # Rewrite with a newer mtime so hot-reload picks it up
    import time
    time.sleep(0.05)
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-4o": {"gi": 90.0}},
    }))
    # resolve_gi → _ensure_loaded should reload without force=True
    assert gi.resolve_gi("x", "gpt-4o") == (90.0, "snapshot")


def test_overrides_reload_when_file_mtime_changes(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({"version": 1, "models": {}}))
    ov = tmp_path / "gi_overrides.json"
    ov.write_text(json.dumps({
        "overrides": {"gemini|flash": {"gi": 33.0}},
    }))
    gi.load_snapshot(force=True)
    gi.load_overrides(force=True)
    assert gi.resolve_gi("gemini", "flash") == (33.0, "override")

    import time
    time.sleep(0.05)
    ov.write_text(json.dumps({
        "overrides": {"gemini|flash": {"gi": 77.0}},
    }))
    assert gi.resolve_gi("gemini", "flash") == (77.0, "override")


def test_reload_scores_forces_fresh_snapshot(tmp_path):
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-4o": {"gi": 40.0}},
    }))
    gi.reload_scores()
    assert gi.resolve_gi("x", "gpt-4o") == (40.0, "snapshot")
    snap.write_text(json.dumps({
        "version": 1,
        "models": {"gpt-4o": {"gi": 95.0}},
    }))
    # Same mtime resolution on some FS can be coarse; force via reload_scores
    gi.reload_scores()
    assert gi.resolve_gi("x", "gpt-4o") == (95.0, "snapshot")
