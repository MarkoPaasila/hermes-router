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
