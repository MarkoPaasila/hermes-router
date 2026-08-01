"""GI must not be persisted in router_state — only feature probes."""
import json

import router


def test_feature_caps_only_strips_gi():
    assert router._feature_caps_only({
        "gi": 88.0,
        "gi_source": "snapshot",
        "rating": 3,
        "supports_tools": False,
        "reasoning": True,
        "extra": "keep",
    }) == {
        "supports_tools": False,
        "reasoning": True,
        "extra": "keep",
    }


def test_model_caps_resolves_gi_live_not_from_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GI_RANKINGS_FILE", str(tmp_path / "gi_rankings.json"))
    monkeypatch.setenv("GI_OVERRIDES_FILE", str(tmp_path / "gi_overrides.json"))
    import gi_ranking as gi
    gi.reset_for_tests()
    (tmp_path / "gi_rankings.json").write_text(json.dumps({
        "version": 1,
        "models": {"flash": {"gi": 60.0}},
    }))
    gi.reload_scores()

    # Stale GI in model_state must be ignored
    router._model_state = {
        ("gemini", "flash"): {
            "gi": 1.0,
            "gi_source": "snapshot",
            "supports_tools": True,
            "reasoning": False,
        },
    }
    caps = router._model_caps("gemini", "flash")
    assert caps["gi"] == 60.0
    assert caps["gi_source"] == "snapshot"
    assert "rating" not in caps
    gi.reset_for_tests()
