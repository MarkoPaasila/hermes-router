from ttft_baseline import TtftBaselineStore, TtftDeadlineExceeded, abort_enabled


def test_cold_deadline_until_min_samples():
    s = TtftBaselineStore(floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=0.2)
    assert s.deadline_s("groq", "llama") == 20.0
    for _ in range(4):
        s.record("groq", "llama", 2.0)
    assert s.deadline_s("groq", "llama") == 20.0  # still cold (n=4)


def test_warm_deadline_max_floor_and_mult_ewma():
    s = TtftBaselineStore(floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=1.0)
    for _ in range(5):
        s.record("a", "m", 1.0)  # ewma ≈ 1.0 with alpha=1
    # max(3.0, 3*1.0) = 3.0
    assert s.deadline_s("a", "m") == 3.0
    s.record("a", "m", 4.0)  # ewma=4 with alpha=1
    assert s.deadline_s("a", "m") == 12.0  # max(3, 3*4)


def test_ewma_smoothing_alpha():
    s = TtftBaselineStore(floor_s=0.0, mult=1.0, min_samples=1, cold_deadline_s=20.0, alpha=0.5)
    s.record("a", "m", 10.0)
    s.record("a", "m", 0.0)
    # ewma = 0.5*0 + 0.5*10 = 5
    assert abs(s.summary("a", "m")["ewma_s"] - 5.0) < 1e-9
    assert s.summary("a", "m")["sample_count"] == 2


def test_candidates_isolated():
    s = TtftBaselineStore(floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=1.0)
    for _ in range(5):
        s.record("groq", "fast", 1.0)
    assert s.deadline_s("groq", "slow") == 20.0


def test_ttft_deadline_exceeded_attrs():
    e = TtftDeadlineExceeded(8.0, 9.2)
    assert e.deadline_s == 8.0
    assert e.waited_s == 9.2


def test_abort_enabled(monkeypatch):
    monkeypatch.setenv("TTFT_ABORT_ENABLED", "0")
    assert abort_enabled() is False
    monkeypatch.setenv("TTFT_ABORT_ENABLED", "1")
    assert abort_enabled() is True
