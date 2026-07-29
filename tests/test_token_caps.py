"""Unit tests for TokenCapTracker (effective caps, learning, persistence)."""
import pytest
from token_caps import TokenCapTracker, MIN_CAP, CUT_FACTOR, RAISE_FACTOR, NEAR_CAP_RATIO


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
