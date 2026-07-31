"""Unit tests for TokenCapTracker (effective caps, learning, persistence)."""
import pytest
from token_caps import (
    TokenCapTracker,
    MIN_CAP,
    CUT_FACTOR,
    RAISE_FACTOR,
    NEAR_CAP_RATIO,
    extract_caps_from_model_item,
    classify_token_limit_error,
)


@pytest.fixture
def tracker(tmp_path):
    return TokenCapTracker(state_file=tmp_path / "caps.json", enabled=True)


def test_effective_unset_returns_none(tracker):
    assert tracker.effective_input_cap("groq", "llama", 0) is None
    assert tracker.effective_output_cap("groq", "llama", 0) is None


def test_effective_env_only(tracker):
    assert tracker.effective_input_cap("groq", "llama", 5500) == 5500
    assert tracker.effective_output_cap("cohere", "cmd", 8192) == 8192


def test_effective_tracker_only(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=8000, max_output=4096)
    assert tracker.effective_input_cap("groq", "llama", 0) == 8000
    assert tracker.effective_output_cap("groq", "llama", 0) == 4096


def test_effective_min_of_env_and_tracker(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=8000)
    assert tracker.effective_input_cap("groq", "llama", 5500) == 5500
    tracker.seed_from_metadata("groq", "llama", max_input=4000)
    assert tracker.effective_input_cap("groq", "llama", 5500) == 4000


def test_disabled_ignores_tracker_values(tmp_path):
    t = TokenCapTracker(state_file=tmp_path / "caps.json", enabled=False)
    t.seed_from_metadata("groq", "llama", max_input=4000)
    assert t.effective_input_cap("groq", "llama", 5500) == 5500
    assert t.effective_input_cap("groq", "llama", 0) is None


def test_failure_cuts_cap(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=10000)
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=9000)
    expected = max(MIN_CAP, int(9000 * CUT_FACTOR))
    assert tracker.effective_input_cap("groq", "llama", 0) == expected


def test_failure_never_raises(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=5000)
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=8000)
    # new_cap = min(prior, max(MIN_CAP, int(observed * CUT_FACTOR)))
    assert tracker.effective_input_cap("groq", "llama", 0) <= 5000


def test_success_near_cap_raises(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=1000)
    used = int(1000 * NEAR_CAP_RATIO)
    tracker.on_success_near_cap("groq", "llama", "input", used_tokens=used)
    assert tracker.effective_input_cap("groq", "llama", 0) == int(1000 * RAISE_FACTOR)


def test_success_far_below_does_not_raise(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=1000)
    tracker.on_success_near_cap("groq", "llama", "input", used_tokens=100)
    assert tracker.effective_input_cap("groq", "llama", 0) == 1000


def test_success_near_cap_uses_env_bound_for_threshold(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=8192)
    tracker.on_success_near_cap("groq", "llama", "input", used_tokens=5000, env_bound=5500)
    assert tracker.effective_input_cap("groq", "llama", 0) == int(8192 * RAISE_FACTOR)
    assert tracker.effective_input_cap("groq", "llama", 5500) == 5500


def test_seed_does_not_loosen_tighter_learned(tracker):
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=4000)
    learned = tracker.effective_input_cap("groq", "llama", 0)
    tracker.seed_from_metadata("groq", "llama", max_input=20000)
    assert tracker.effective_input_cap("groq", "llama", 0) == learned


def test_persist_round_trip(tmp_path):
    path = tmp_path / "caps.json"
    t1 = TokenCapTracker(state_file=path, enabled=True)
    t1.seed_from_metadata("cerebras", "llama3.1-8b", max_input=8192, max_output=2048)
    t1.flush()
    t2 = TokenCapTracker(state_file=path, enabled=True)
    t2.load()
    assert t2.effective_input_cap("cerebras", "llama3.1-8b", 0) == 8192
    assert t2.effective_output_cap("cerebras", "llama3.1-8b", 0) == 2048
    snap = t2.snapshot("cerebras", "llama3.1-8b")
    assert snap["source"] == "metadata"
    assert "updated_at" in snap


def test_corrupt_file_fail_soft(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text("{not json")
    t = TokenCapTracker(state_file=path, enabled=True)
    t.load()  # must not raise
    assert t.effective_input_cap("x", "y", 0) is None


def test_extract_context_length():
    assert extract_caps_from_model_item({"id": "m", "context_length": 8192}) == (8192, None)


def test_extract_max_model_len_and_output():
    item = {"id": "m", "max_model_len": 32768, "max_completion_tokens": 4096}
    assert extract_caps_from_model_item(item) == (32768, 4096)


def test_extract_nested_top_provider():
    item = {
        "id": "m",
        "top_provider": {"context_length": 128000, "max_completion_tokens": 16384},
    }
    assert extract_caps_from_model_item(item) == (128000, 16384)


def test_extract_missing_returns_nones():
    assert extract_caps_from_model_item({"id": "m"}) == (None, None)


def test_classify_413_as_input():
    assert classify_token_limit_error(413, "Payload Too Large") == "input"


def test_classify_400_context_length():
    body = "This model's maximum context length is 8192 tokens"
    assert classify_token_limit_error(400, body, est_tokens=9000) == "input"


def test_classify_400_max_tokens_output():
    body = "max_tokens is too large: 65536"
    assert classify_token_limit_error(400, body, requested_max_tokens=65536) == "output"


def test_classify_unrelated_400_returns_none():
    assert classify_token_limit_error(400, "invalid tool schema") is None


def test_classify_ambiguous_uses_heuristics():
    body = "too many tokens"
    assert classify_token_limit_error(400, body, est_tokens=12000, requested_max_tokens=256) == "input"
    assert classify_token_limit_error(400, body, est_tokens=100, requested_max_tokens=100000) == "output"


def test_metadata_seed_sets_low_confidence(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=8000, max_output=4096)
    snap = tracker.snapshot("groq", "llama")
    assert snap["input_confidence"] == pytest.approx(0.3)
    assert snap["output_confidence"] == pytest.approx(0.3)
    assert tracker.hard_input_cap("groq", "llama", 0) is None
    assert tracker.hard_output_cap("groq", "llama", 0) is None


def test_failure_raises_confidence_to_hard(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=10000)
    tracker.on_token_limit_failure("groq", "llama", "input", observed_tokens=9000)
    snap = tracker.snapshot("groq", "llama")
    assert snap["input_confidence"] == pytest.approx(0.7)
    expected = max(MIN_CAP, int(9000 * CUT_FACTOR))
    assert tracker.hard_input_cap("groq", "llama", 0) == expected
    assert tracker.hard_input_cap("groq", "llama", 1000) == 1000


def test_success_near_cap_bumps_confidence(tracker):
    tracker.seed_from_metadata("groq", "llama", max_input=1000)
    used = int(1000 * NEAR_CAP_RATIO)
    tracker.on_success_near_cap("groq", "llama", "input", used_tokens=used)
    assert tracker.snapshot("groq", "llama")["input_confidence"] == pytest.approx(0.4)
    assert tracker.hard_input_cap("groq", "llama", 0) is None


def test_migrate_legacy_source_to_confidence(tmp_path):
    path = tmp_path / "caps.json"
    path.write_text(
        '{"models":{'
        '"a::m1":{"max_input":100,"source":"learned","updated_at":1},'
        '"a::m2":{"max_input":100,"source":"mixed","updated_at":1},'
        '"a::m3":{"max_input":100,"source":"metadata","updated_at":1}'
        "}}"
    )
    t = TokenCapTracker(state_file=path, enabled=True)
    t.load()
    assert t.snapshot("a", "m1")["input_confidence"] == pytest.approx(0.85)
    assert t.hard_input_cap("a", "m1", 0) == 100
    assert t.snapshot("a", "m2")["input_confidence"] == pytest.approx(0.6)
    assert t.hard_input_cap("a", "m2", 0) is None
    assert t.snapshot("a", "m3")["input_confidence"] == pytest.approx(0.3)
    assert t.hard_input_cap("a", "m3", 0) is None


def test_persist_round_trip_includes_confidence(tmp_path):
    path = tmp_path / "caps.json"
    t1 = TokenCapTracker(state_file=path, enabled=True)
    t1.seed_from_metadata("cerebras", "llama", max_input=8192)
    t1.on_token_limit_failure("cerebras", "llama", "input", observed_tokens=4000)
    t1.flush()
    t2 = TokenCapTracker(state_file=path, enabled=True)
    t2.load()
    snap = t2.snapshot("cerebras", "llama")
    assert snap["input_confidence"] == pytest.approx(0.7)
    assert t2.hard_input_cap("cerebras", "llama", 0) is not None
