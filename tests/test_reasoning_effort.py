"""Normalize client reasoning_effort aliases (max → high) before upstream."""
import router


def test_normalize_reasoning_effort_top_level_max():
    body = {"reasoning_effort": "max", "model": "m"}
    router._normalize_reasoning_effort(body)
    assert body["reasoning_effort"] == "high"


def test_normalize_reasoning_effort_nested():
    body = {"reasoning": {"effort": "MAX"}, "model": "m"}
    router._normalize_reasoning_effort(body)
    assert body["reasoning"]["effort"] == "high"


def test_normalize_reasoning_effort_leaves_known_values():
    for val in ("high", "medium", "low", "minimal", "none"):
        body = {"reasoning_effort": val}
        router._normalize_reasoning_effort(body)
        assert body["reasoning_effort"] == val


def test_normalize_reasoning_effort_aliases():
    for alias in ("xhigh", "ultra", " Max "):
        body = {"reasoning_effort": alias}
        router._normalize_reasoning_effort(body)
        assert body["reasoning_effort"] == "high"


def test_to_codex_body_maps_max_effort():
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "max",
    }
    body = router._to_codex_body(payload, "gpt-5")
    assert body["reasoning"]["effort"] == "high"
