import time, pytest
from rate_limiter import TokenBucket, BucketGroup, LIMIT_KEYS, PROVIDER_RATE_DEFAULTS

WINDOW = 60.0  # 1-minute bucket

def make_bucket(cap=10.0, tokens=None):
    return TokenBucket(window_seconds=WINDOW, cap=cap,
                       tokens=cap if tokens is None else tokens)

def test_consume_success():
    b = make_bucket(cap=10.0, tokens=10.0)
    assert b.consume(1.0) is True
    assert abs(b.tokens - 9.0) < 0.01

def test_consume_fails_when_empty():
    b = make_bucket(cap=10.0, tokens=0.0)
    assert b.consume(1.0) is False

def test_refill_adds_tokens():
    b = make_bucket(cap=60.0, tokens=0.0)
    b.last_refill = time.time() - 30  # 30s elapsed
    b.refill(time.time())
    assert abs(b.tokens - 30.0) < 0.5   # 30s * (60/60) = 30

def test_refill_clamps_to_cap():
    b = make_bucket(cap=10.0, tokens=9.0)
    b.last_refill = time.time() - 100
    b.refill(time.time())
    assert b.tokens == 10.0

def test_headroom():
    b = make_bucket(cap=10.0, tokens=5.0)
    assert abs(b.headroom() - 0.5) < 0.01

def test_headroom_zero_cap():
    b = make_bucket(cap=0.0, tokens=0.0)
    assert b.headroom() == 1.0

def test_time_to_refill_already_enough():
    b = make_bucket(cap=10.0, tokens=5.0)
    assert b.time_to_refill(3.0) == 0.0

def test_time_to_refill_calculates():
    b = make_bucket(cap=60.0, tokens=0.0)
    # rate = 60/60 = 1 tok/s; need 10 more → 10s
    assert abs(b.time_to_refill(10.0) - 10.0) < 0.1

def test_on_429_with_history_cuts_cap():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0)
    assert b.cap == pytest.approx(8.0)   # 10 * 0.8
    assert b.tokens == 0.0

def test_on_429_without_history_halves():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 1.0
    b.on_429(observed_rate=1.0)
    assert b.cap == pytest.approx(30.0)  # cap * 0.5

def test_on_success_nudge():
    b = make_bucket(cap=10.0, tokens=10.0)
    b._initial_cap = 10.0
    for _ in range(20):
        b.on_success()
    assert b.cap > 10.0

def test_set_from_header():
    b = make_bucket(cap=10.0, tokens=5.0)
    b.set_from_header(cap=100.0, remaining=80.0)
    assert b.cap == 100.0
    assert b.tokens == 80.0

def test_restore_clamps():
    b = make_bucket(cap=10.0, tokens=9.0)
    b.restore(5.0)
    assert b.tokens == 10.0

def test_inactive_after_quiet_period():
    b = make_bucket(cap=100.0, tokens=100.0)
    b.check_inactive(requests_this_period=2)   # < max(10, 100*0.1)=10
    assert b.active is False

def test_stays_active_when_busy():
    b = make_bucket(cap=100.0, tokens=50.0)
    b.check_inactive(requests_this_period=20)
    assert b.active is True

def test_to_dict_roundtrip():
    b = make_bucket(cap=30.0, tokens=15.0)
    b.last_refill = 1000.0
    d = b.to_dict()
    b2 = TokenBucket.from_dict(d, window_seconds=WINDOW, initial_cap=30.0)
    assert b2.cap == 30.0
    assert b2.tokens == 15.0


def test_bucketgroup_consume_passes():
    g = BucketGroup(provider_name="groq")
    ok, wait = g.consume(req_count=1.0, token_count=100.0)
    assert ok is True
    assert wait == 0.0

def test_bucketgroup_consume_fails_when_rpm_empty():
    g = BucketGroup(provider_name="groq")
    rpm = g.buckets.get("RPM")
    if rpm:
        rpm.tokens = 0.0
        ok, wait = g.consume(req_count=1.0, token_count=0.0)
        assert ok is False
        assert wait > 0

def test_bucketgroup_consume_returns_max_wait_across_failing_buckets():
    g = BucketGroup(provider_name="groq", caps={"RPM": 60.0, "TPM": 6000.0})
    rpm, tpm = g.buckets["RPM"], g.buckets["TPM"]
    rpm.tokens = 0.0   # rate 1 req/s → 1s wait for 1 req
    tpm.tokens = 0.0   # rate 100 tok/s → 10s wait for 1000 tok
    ok, wait = g.consume(req_count=1.0, token_count=1000.0)
    assert ok is False
    assert wait == pytest.approx(10.0, abs=0.1)

def test_bucketgroup_consume_atomic_no_partial_debit():
    g = BucketGroup(provider_name="groq", caps={"RPM": 30.0, "TPM": 6000.0})
    rpm, tpm = g.buckets["RPM"], g.buckets["TPM"]
    rpm.tokens = 30.0
    tpm.tokens = 0.0
    rpm_before = rpm.tokens
    ok, _ = g.consume(req_count=1.0, token_count=100.0)
    assert ok is False
    assert rpm.tokens == rpm_before

def test_bucketgroup_headroom_all_full():
    g = BucketGroup(provider_name="groq")
    assert g.headroom() == pytest.approx(1.0, abs=0.05)

def test_bucketgroup_restore_tokens():
    g = BucketGroup(provider_name="groq")
    tpm = g.buckets.get("TPM")
    if tpm:
        tpm.tokens = 0.0
        g.restore_tokens(100.0)
        assert tpm.tokens == pytest.approx(100.0)

def test_bucketgroup_update_from_headers():
    g = BucketGroup(provider_name="groq")
    g.update_from_headers({
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-remaining-requests": "45",
    })
    rpm = g.buckets.get("RPM")
    assert rpm is not None
    assert rpm.cap == 60.0
    assert rpm.tokens == 45.0

def test_bucketgroup_on_429_cuts_caps():
    g = BucketGroup(provider_name="groq")
    rpm = g.buckets.get("RPM")
    if rpm:
        original_cap = rpm.cap
        rpm._period_consumed = 10.0
        g.on_429({})
        assert rpm.cap < original_cap

def test_bucketgroup_to_dict_roundtrip():
    g = BucketGroup(provider_name="groq")
    d = g.to_dict()
    g2 = BucketGroup.from_dict(d, provider_name="groq")
    assert set(g2.buckets.keys()) == set(d.keys())

def test_default_caps_table_has_groq():
    assert "groq" in PROVIDER_RATE_DEFAULTS
    assert PROVIDER_RATE_DEFAULTS["groq"]["RPM"] == 30

def test_default_caps_fallback():
    assert "_default" in PROVIDER_RATE_DEFAULTS


import tempfile
from pathlib import Path
from rate_limiter import AdaptiveRateLimiter

def make_limiter(tmp_path):
    return AdaptiveRateLimiter(state_file=Path(tmp_path) / "rate_limits_state.json")

def test_check_and_consume_passes(tmp_path):
    rl = make_limiter(tmp_path)
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 100.0)
    assert ok is True

def test_check_and_consume_fails_when_rpm_full(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    if "RPM" in g.buckets:
        g.buckets["RPM"].tokens = 0.0
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 100.0)
    assert ok is False

def test_headroom_returns_float(tmp_path):
    rl = make_limiter(tmp_path)
    h = rl.headroom("groq", "key-abc12345", "llama")
    assert 0.0 <= h <= 1.0

def test_on_429_updates_buckets(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    if "RPM" in g.buckets:
        original = g.buckets["RPM"].cap
        g.buckets["RPM"]._period_consumed = 10.0
        rl.on_429("groq", "key-abc12345", "llama", {})
        assert g.buckets["RPM"].cap <= original

def test_flush_and_load(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    rl.flush()
    state_file = Path(tmp_path) / "rate_limits_state.json"
    assert state_file.exists()
    rl2 = make_limiter(tmp_path)
    rl2.load()
    h = rl2.headroom("groq", "key-abc12345", "llama")
    assert 0.0 <= h <= 1.0

def test_snapshot_structure(tmp_path):
    rl = make_limiter(tmp_path)
    snap = rl.snapshot("groq", "key-abc12345", "llama")
    assert "provider_wide" in snap
    assert "model" in snap

def test_check_and_consume_rollback_restores_all_pw_r_buckets(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    req_count, token_count = 1.0, 100.0
    r_before = {
        name: b.tokens
        for name, b in pw.buckets.items()
        if b.active and LIMIT_KEYS.get(name, ("?",))[0] == "R"
    }
    assert r_before, "expected at least one active R-bucket on provider-wide group"
    if "RPM" in mg.buckets:
        mg.buckets["RPM"].tokens = 0.0
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", req_count, token_count)
    assert ok is False
    assert wait > 0
    for name, before in r_before.items():
        assert pw.buckets[name].tokens == pytest.approx(before)

def test_check_and_consume_rollback_restores_requests_this_period(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    before = pw._requests_this_period
    if "RPM" in mg.buckets:
        mg.buckets["RPM"].tokens = 0.0
    ok, _ = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 100.0)
    assert ok is False
    assert pw._requests_this_period == before
