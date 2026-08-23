import csv
import json, time, pytest
import rate_limiter
from rate_limiter import (
    TokenBucket, BucketGroup, LIMIT_KEYS, PROVIDER_RATE_DEFAULTS,
    BucketEventCsv, CSV_COLUMNS, _truthy_env,
)

WINDOW = 60.0  # 1-minute bucket

def make_bucket(cap=10.0, tokens=None):
    return TokenBucket(window_seconds=WINDOW, cap=cap,
                       tokens=cap if tokens is None else tokens)

def test_new_bucket_starts_at_half_fill():
    b = TokenBucket(window_seconds=WINDOW, cap=100.0)
    assert b.tokens == pytest.approx(50.0)

def test_explicit_tokens_unchanged():
    b = TokenBucket(window_seconds=WINDOW, cap=100.0, tokens=100.0)
    assert b.tokens == pytest.approx(100.0)

def test_bucketgroup_defaults_half_fill():
    g = BucketGroup(provider_name="mistral", caps={"RPM": 10.0, "TPM": 1000.0})
    assert g.buckets["RPM"].tokens == pytest.approx(5.0)
    assert g.buckets["TPM"].tokens == pytest.approx(500.0)

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
    assert b.tokens == pytest.approx(8.0)  # keep fill, clamped to new cap

def test_on_429_without_history_halves():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 1.0
    b.on_429(observed_rate=1.0)
    assert b.cap == pytest.approx(30.0)  # cap * 0.5

def test_on_429_soft_with_history():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0, soft=True)
    # Soft cuts floor at RATE_LEARN_SOFT_FLOOR_FRAC * _floor_cap (0.5 * 60 = 30)
    assert b.cap == pytest.approx(30.0)
    # Soft path keeps tokens (clamped to new cap); hard cuts do the same.
    assert b.tokens == pytest.approx(30.0)


def test_on_429_soft_without_history():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 1.0
    b.on_429(observed_rate=1.0, soft=True)
    # Default RATE_LEARN_SOFT_CUT_FACTOR = 0.9 → 54, above soft floor 30
    assert b.cap == pytest.approx(54.0)
    assert b.tokens == pytest.approx(30.0)


def test_bucketgroup_consume_lifts_cap_when_request_exceeds_tpm():
    """Large agent contexts must not soft-lock when need > guessed TPM."""
    g = BucketGroup(provider_name="mistral", caps={"RPM": 5.0, "TPM": 16_000.0})
    ok, wait, _ = g.consume(req_count=1.0, token_count=71_621.0)
    assert ok is True
    assert wait == 0.0
    # Burst ×2 so one debit does not leave ~0% headroom for the next turn
    assert g.buckets["TPM"].cap == pytest.approx(71_621.0 * 2.0)
    assert g.buckets["TPM"].tokens == pytest.approx(71_621.0)


def test_bucketgroup_second_large_request_fits_after_burst():
    g = BucketGroup(provider_name="mistral", caps={"RPM": 5.0, "TPM": 16_000.0})
    assert g.consume(1.0, 71_621.0)[0] is True
    # After burst×2, ~50% headroom remains for a same-sized follow-up.
    assert g.buckets["TPM"].headroom() == pytest.approx(0.5, abs=0.01)
    ok, wait, _ = g.consume(1.0, 71_621.0)
    assert ok is True
    assert wait == pytest.approx(0.0, abs=0.1)
    assert g.buckets["TPM"].tokens == pytest.approx(0.0, abs=1.0)


def test_load_migrates_poisoned_provider_wide_caps(tmp_path):
    state = tmp_path / "rate.json"
    state.write_text(json.dumps({
        "version": 1,
        "groups": {
            "provider:gemini|key:deadbeef": {
                "RPM": {"cap": 1.0, "tokens": 1.0, "last_refill": time.time()},
                "TPM": {"cap": 97.2, "tokens": 97.2, "last_refill": time.time()},
            }
        },
    }))
    rl = rate_limiter.AdaptiveRateLimiter(state_file=state)
    rl.load()
    g = rl.get_group("gemini", "xxdeadbeef", None)
    assert g.buckets["RPM"].cap == pytest.approx(150.0)
    assert g.buckets["TPM"].cap == pytest.approx(2_500_000.0)


def test_on_429_hard_unchanged_default():
    b = make_bucket(cap=60.0, tokens=30.0)
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0)  # soft=False default
    assert b.cap == pytest.approx(8.0)


def test_on_success_custom_streak_and_nudge():
    b = make_bucket(cap=10.0, tokens=10.0)
    for _ in range(10):
        b.on_success(streak=10, nudge_pct=8.0)
    assert b.cap == pytest.approx(10.0 * 1.08)

def test_on_success_nudge():
    b = make_bucket(cap=10.0, tokens=10.0)
    b._initial_cap = 10.0
    for _ in range(20):
        b.on_success()
    assert b.cap > 10.0
    # Opened capacity is granted so headroom % does not collapse.
    assert b.tokens == pytest.approx(b.cap)


def test_on_success_nudge_grants_opened_capacity():
    b = make_bucket(cap=100.0, tokens=50.0)
    for _ in range(rate_limiter.RATE_LEARN_SUCCESS_STREAK):
        b.on_success()
    assert b.cap == pytest.approx(105.0)
    assert b.tokens == pytest.approx(55.0)  # 50 + 5 granted
    assert b.headroom() == pytest.approx(55.0 / 105.0, abs=0.01)


def test_on_success_nudge_past_former_ceiling():
    """Caps keep growing even above the old 10× initial-default ceiling."""
    b = make_bucket(cap=10.0, tokens=10.0)
    b._initial_cap = 10.0
    b.cap = 110.0  # already above 10 × initial
    before = b.cap
    for _ in range(20):
        b.on_success()
    assert b.cap > before
    assert b.cap == pytest.approx(before * 1.05)

def test_set_from_header():
    b = make_bucket(cap=10.0, tokens=5.0)
    b.set_from_header(cap=100.0, remaining=80.0)
    assert b.cap == 100.0
    assert b.tokens == 80.0

def test_header_pin_blocks_on_success_nudge():
    b = make_bucket(cap=100.0, tokens=100.0)
    b.set_from_header(cap=100.0, remaining=80.0)
    assert b._header_pinned is True
    for _ in range(rate_limiter.RATE_LEARN_SUCCESS_STREAK):
        b.on_success()
    assert b.cap == pytest.approx(100.0)

def test_non_header_429_clears_pin_and_cuts():
    b = make_bucket(cap=100.0, tokens=50.0)
    b.set_from_header(cap=100.0, remaining=50.0)
    assert b._header_pinned is True
    b._period_consumed = 10.0
    b.on_429(observed_rate=10.0)
    assert b._header_pinned is False
    assert b.cap == pytest.approx(8.0)  # 10 * 0.8
    assert b.tokens == pytest.approx(8.0)  # clamped remaining, not hard-zeroed

def test_older_observed_at_ignored():
    b = make_bucket(cap=100.0, tokens=50.0)
    assert b.set_from_header(cap=100.0, remaining=40.0, observed_at=200.0) is None
    assert b.tokens == pytest.approx(40.0)
    assert b.set_from_header(cap=100.0, remaining=90.0, observed_at=100.0) is None
    assert b.tokens == pytest.approx(40.0)
    assert b._header_obs_at == pytest.approx(200.0)

def test_newer_observed_at_applies():
    b = make_bucket(cap=100.0, tokens=50.0)
    assert b.set_from_header(cap=100.0, remaining=40.0, observed_at=100.0) is None
    assert b.set_from_header(cap=100.0, remaining=20.0, observed_at=150.0) is None
    assert b.tokens == pytest.approx(20.0)
    assert b._header_obs_at == pytest.approx(150.0)

def test_bucketgroup_headers_respect_observed_at():
    g = BucketGroup(provider_name="groq", caps={"RPM": 30.0, "TPM": 6000.0})
    g.update_from_headers({
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-remaining-requests": "10",
    }, observed_at=100.0)
    assert g.buckets["RPM"].tokens == pytest.approx(10.0)
    g.update_from_headers({
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-remaining-requests": "25",
    }, observed_at=50.0)
    assert g.buckets["RPM"].tokens == pytest.approx(10.0)

def test_restore_clamps():
    b = make_bucket(cap=10.0, tokens=9.0)
    b.restore(5.0)
    assert b.tokens == 10.0

def test_inactive_after_quiet_period():
    # Full-grid design: long windows stay active for debit/rank even when quiet.
    b = TokenBucket(window_seconds=86400.0, cap=100.0, tokens=100.0)
    b.check_inactive(activity=2)   # < max(10, 100*0.1)=10
    assert b.active is True


def test_check_inactive_never_deactivates_long_window():
    b = TokenBucket(window_seconds=86400.0, cap=100.0, tokens=100.0)
    b.check_inactive(activity=0)
    assert b.active is True

def test_stays_active_when_busy():
    b = TokenBucket(window_seconds=86400.0, cap=100.0, tokens=50.0)
    b.check_inactive(activity=20)
    assert b.active is True

def test_minute_bucket_never_auto_inactive():
    b = TokenBucket(window_seconds=60.0, cap=100.0, tokens=100.0)
    b.check_inactive(activity=0)
    assert b.active is True


def test_headroom_does_not_create_groups(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl._groups == {}
    h = rl.headroom("groq", "key-abc12345", "llama")
    assert h == 1.0
    assert rl._groups == {}

def test_to_dict_roundtrip():
    b = make_bucket(cap=30.0, tokens=15.0)
    b.last_refill = 1000.0
    d = b.to_dict()
    b2 = TokenBucket.from_dict(d, window_seconds=WINDOW, initial_cap=30.0)
    assert b2.cap == 30.0
    assert b2.tokens == 15.0


def test_bucketgroup_consume_passes():
    g = BucketGroup(provider_name="groq")
    ok, wait, _ = g.consume(req_count=1.0, token_count=100.0)
    assert ok is True
    assert wait == 0.0

def test_bucketgroup_consume_fails_when_rpm_empty():
    g = BucketGroup(provider_name="groq")
    rpm = g.buckets.get("RPM")
    if rpm:
        rpm.tokens = 0.0
        ok, wait, _ = g.consume(req_count=1.0, token_count=0.0)
        assert ok is False
        assert wait > 0

def test_bucketgroup_consume_returns_max_wait_across_failing_buckets():
    g = BucketGroup(provider_name="groq", caps={"RPM": 60.0, "TPM": 6000.0})
    rpm, tpm = g.buckets["RPM"], g.buckets["TPM"]
    rpm.tokens = 0.0   # rate 1 req/s → 1s wait for 1 req
    tpm.tokens = 0.0   # rate 100 tok/s → 10s wait for 1000 tok
    ok, wait, _ = g.consume(req_count=1.0, token_count=1000.0)
    assert ok is False
    assert wait == pytest.approx(10.0, abs=0.1)

def test_bucketgroup_consume_atomic_no_partial_debit():
    g = BucketGroup(provider_name="groq", caps={"RPM": 30.0, "TPM": 6000.0})
    rpm, tpm = g.buckets["RPM"], g.buckets["TPM"]
    rpm.tokens = 30.0
    tpm.tokens = 0.0
    rpm_before = rpm.tokens
    ok, _, _ = g.consume(req_count=1.0, token_count=100.0)
    assert ok is False
    assert rpm.tokens == rpm_before

def test_bucketgroup_headroom_all_full():
    g = BucketGroup(provider_name="groq")
    for b in g.buckets.values():
        b.tokens = b.cap
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
        rpm.tokens = 0.0
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


def test_truthy_env(monkeypatch):
    monkeypatch.setenv("RATE_BUCKET_CSV_ENABLED", "YES")
    assert _truthy_env("RATE_BUCKET_CSV_ENABLED") is True
    monkeypatch.setenv("RATE_BUCKET_CSV_ENABLED", "0")
    assert _truthy_env("RATE_BUCKET_CSV_ENABLED") is False


def test_csv_disabled_writes_nothing(tmp_path):
    path = tmp_path / "events.csv"
    w = BucketEventCsv(enabled=False, path=path)
    w.record(provider="groq", key_hint="deadbeef", model="llama",
             scope="model", bucket="RPM", event="nudge", reason="success_streak",
             cap=31.5, old_cap=30.0, tokens=10.0, used=21.5, headroom=10 / 31.5)
    assert not path.exists()


def test_csv_writes_header_once_then_appends(tmp_path):
    path = tmp_path / "events.csv"
    w = BucketEventCsv(enabled=True, path=path)
    kwargs = dict(provider="groq", key_hint="deadbeef", model="llama",
                  scope="model", bucket="RPM", event="nudge", reason="success_streak",
                  cap=31.5, old_cap=30.0, tokens=10.0, used=21.5, headroom=10 / 31.5)
    w.record(**kwargs)
    w.record(**{**kwargs, "cap": 33.0, "old_cap": 31.5})
    text = path.read_text()
    assert text.count(",".join(CSV_COLUMNS)) == 1
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 2
    assert list(rows[0].keys()) == CSV_COLUMNS
    assert rows[0]["datetime"]  # non-empty ISO local
    assert rows[0]["provider"] == "groq"
    assert rows[0]["bucket"] == "RPM"
    assert float(rows[0]["old_cap"]) == 30.0
    assert float(rows[1]["cap"]) == 33.0


def test_csv_soft_fails_on_unwritable(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("not-a-dir")
    path = blocker / "events.csv"  # parent is a file → mkdir/open fails
    w = BucketEventCsv(enabled=True, path=path)
    w.record(provider="groq", key_hint="deadbeef", model="",
             scope="provider_wide", bucket="TPM", event="cut", reason="soft_429",
             cap=100.0, old_cap=200.0, tokens=0.0, used=100.0, headroom=0.0)


import json
import tempfile
from pathlib import Path
from rate_limiter import AdaptiveRateLimiter

def make_limiter(tmp_path):
    return AdaptiveRateLimiter(state_file=Path(tmp_path) / "rate_limits_state.json")

def test_reconcile_restores_surplus(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 8000.0)[0]
    h_reserved = rl.headroom("openrouter", "key-abc12345", "m")
    rl.reconcile("openrouter", "key-abc12345", "m", 8000.0, 2000.0)
    assert rl.headroom("openrouter", "key-abc12345", "m") > h_reserved + 0.2

def test_reconcile_debits_deficit(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 4000.0)[0]
    h_reserved = rl.headroom("openrouter", "key-abc12345", "m")
    rl.reconcile("openrouter", "key-abc12345", "m", 4000.0, 12000.0)
    assert rl.headroom("openrouter", "key-abc12345", "m") < h_reserved - 0.2

def test_release_reservation_restores_both_scopes(tmp_path):
    rl = make_limiter(tmp_path)
    key, model = "key-abc12345", "llama"
    pw = rl.get_group("groq", key, None)
    mg = rl.get_group("groq", key, model)
    for g in (pw, mg):
        for b in g.buckets.values():
            b.tokens = b.cap
            b.last_refill = time.time()
    req_count, token_count = 1.0, 500.0
    pw_rpm_before = pw.buckets["RPM"].tokens
    pw_tpm_before = pw.buckets["TPM"].tokens
    mg_rpm_before = mg.buckets["RPM"].tokens
    mg_tpm_before = mg.buckets["TPM"].tokens
    assert rl.check_and_consume("groq", key, model, req_count, token_count)[0]
    assert pw.buckets["RPM"].tokens == pytest.approx(pw_rpm_before - req_count)
    assert mg.buckets["TPM"].tokens == pytest.approx(mg_tpm_before - token_count)
    rl.release_reservation("groq", key, model, req_count, token_count)
    assert pw.buckets["RPM"].tokens == pytest.approx(pw_rpm_before)
    assert pw.buckets["TPM"].tokens == pytest.approx(pw_tpm_before)
    assert mg.buckets["RPM"].tokens == pytest.approx(mg_rpm_before)
    assert mg.buckets["TPM"].tokens == pytest.approx(mg_tpm_before)

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
    for b in pw.buckets.values():
        b.tokens = b.cap
        b.last_refill = time.time()
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

def test_run_all_inactive_checks_no_error(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    rl.run_all_inactive_checks()  # must not raise

def test_run_all_inactive_checks_marks_quiet_buckets_inactive(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    # Force all buckets to be quiet (never hit zero, zero requests)
    for b in g.buckets.values():
        b._hit_zero = False
        b._period_consumed = 0.0
    g._requests_this_period = 0
    rl.run_all_inactive_checks()
    assert all(b.active for b in g.buckets.values())

def test_clear_group_removes_and_flushes(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    gk = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    assert gk in rl._groups or AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama") in rl._groups
    # Clear the provider-wide group that check_and_consume created
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    assert rl.clear_group(pw) is True
    assert pw not in rl._groups
    # Disk no longer contains pw
    rl.flush()  # ensure file readable; clear_group already flushed
    doc = json.loads((Path(tmp_path) / "rate_limits_state.json").read_text())
    assert pw not in (doc.get("groups") or {})

def test_clear_group_unknown_returns_false(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl.clear_group("provider:nope|key:deadbeef") is False

def test_parse_group_key_provider_wide():
    d = AdaptiveRateLimiter.parse_group_key("provider:groq|key:abc12345")
    assert d == {"provider": "groq", "key_hint": "abc12345", "model": None}

def test_parse_group_key_model():
    d = AdaptiveRateLimiter.parse_group_key("provider:groq|key:abc12345|model:llama")
    assert d == {"provider": "groq", "key_hint": "abc12345", "model": "llama"}

def test_parse_group_key_malformed():
    assert AdaptiveRateLimiter.parse_group_key("not-a-key") is None

def test_list_groups_includes_role(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    rows = rl.list_groups(include_orphans=True, configured_ids={pw, mg})
    by_id = {r["id"]: r for r in rows}
    assert by_id[pw]["role"] == "estimate"
    assert by_id[mg]["role"] == "authoritative"

def test_list_groups_filters_orphans(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    # Only mark provider-wide as configured
    rows = rl.list_groups(include_orphans=False, configured_ids={pw})
    ids = {r["id"] for r in rows}
    assert pw in ids
    assert mg not in ids
    rows_all = rl.list_groups(include_orphans=True, configured_ids={pw})
    ids_all = {r["id"] for r in rows_all}
    assert pw in ids_all and mg in ids_all
    by_id = {r["id"]: r for r in rows_all}
    assert by_id[pw]["configured"] is True
    assert by_id[mg]["configured"] is False
    assert by_id[pw]["scope"] == "provider_wide"
    assert by_id[mg]["scope"] == "model"
    assert by_id[mg]["model"] == "llama"
    assert "RPM" in by_id[pw]["buckets"] or len(by_id[pw]["buckets"]) >= 1
    b = next(iter(by_id[pw]["buckets"].values()))
    assert set(b) >= {"cap", "used", "tokens", "headroom", "active"}

def test_list_groups_headroom_null_when_no_active(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    for b in g.buckets.values():
        b.active = False
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    rows = rl.list_groups(include_orphans=True, configured_ids={pw})
    row = next(r for r in rows if r["id"] == pw)
    assert row["headroom"] is None
    assert row["binding"] is None


def test_list_groups_hides_dormant_by_default(tmp_path):
    rl = make_limiter(tmp_path)
    g = rl.get_group("groq", "key-abc12345", None)
    for b in g.buckets.values():
        b.active = False
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    hidden = rl.list_groups(include_orphans=False, configured_ids={pw})
    assert all(r["id"] != pw for r in hidden)
    shown = rl.list_groups(include_orphans=True, configured_ids={pw})
    assert any(r["id"] == pw for r in shown)


def test_snapshot_does_not_create_groups(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl._groups == {}
    snap = rl.snapshot("groq", "key-abc12345", "llama")
    assert snap["provider_wide"] == {}
    assert snap["model"] == {}
    assert snap["blocked_until"] is None
    assert rl._groups == {}


def test_on_429_with_retry_after_blocks_consume(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    # Prime groups so on_429 has buckets to update
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    rl.on_429("groq", "key-abc12345", "llama", {"Retry-After": "30"})
    # Cap learning zeros tokens on both groups; refill so only model blocked_until binds
    for g in (rl.get_group("groq", "key-abc12345", None),
              rl.get_group("groq", "key-abc12345", "llama")):
        for b in g.buckets.values():
            b.tokens = b.cap
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    assert ok is False
    assert wait == pytest.approx(30.0, abs=0.5)


def test_on_429_retry_after_expires_allows_consume(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: now["t"])
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    for g in (rl.get_group("groq", "key-abc12345", None),
              rl.get_group("groq", "key-abc12345", "llama")):
        for b in g.buckets.values():
            b.tokens = b.cap
    rl.on_429("groq", "key-abc12345", "llama", {"Retry-After": "30"})
    now["t"] += 31.0
    for g in (rl.get_group("groq", "key-abc12345", None),
              rl.get_group("groq", "key-abc12345", "llama")):
        for b in g.buckets.values():
            b.tokens = b.cap
            b.last_refill = now["t"]
    ok, wait = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    assert ok is True
    assert wait == 0.0


def test_on_429_without_retry_after_no_long_hold(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    rl.on_429("groq", "key-abc12345", "llama", {})
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    assert pw.blocked_until <= now
    assert mg.blocked_until <= now


def test_blocked_until_persists_when_future(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    rl.on_429("groq", "key-abc12345", "llama", {"Retry-After": "60"})
    rl.flush()
    doc = json.loads((Path(tmp_path) / "rate_limits_state.json").read_text())
    mg = AdaptiveRateLimiter._group_key("groq", "key-abc12345", "llama")
    pw = AdaptiveRateLimiter._group_key("groq", "key-abc12345", None)
    assert doc["groups"][mg].get("blocked_until") == pytest.approx(now + 60.0)
    assert "blocked_until" not in doc["groups"].get(pw, {})
    rl2 = make_limiter(tmp_path)
    monkeypatch.setattr(time, "time", lambda: now)
    rl2.load()
    for g in (rl2.get_group("groq", "key-abc12345", None),
              rl2.get_group("groq", "key-abc12345", "llama")):
        for b in g.buckets.values():
            b.tokens = b.cap
            b.last_refill = now
    ok, wait = rl2.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    assert ok is False
    assert wait == pytest.approx(60.0, abs=0.5)


def test_retry_after_does_not_block_sibling_model(tmp_path, monkeypatch):
    """A Retry-After on one model must not hold sibling models on the same key."""
    rl = make_limiter(tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    rl.check_and_consume("groq", "key-abc12345", "mistral", 1.0, 1.0)
    rl.on_429("groq", "key-abc12345", "llama", {"Retry-After": "30"})
    # Cap learning zeros tokens; refill so Retry-After is the only binder on llama
    for g in (rl.get_group("groq", "key-abc12345", None),
              rl.get_group("groq", "key-abc12345", "llama"),
              rl.get_group("groq", "key-abc12345", "mistral")):
        for b in g.buckets.values():
            b.tokens = b.cap
    # Hit model stays blocked
    ok_hit, wait_hit = rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 1.0)
    assert ok_hit is False
    assert wait_hit == pytest.approx(30.0, abs=0.5)
    assert rl.headroom("groq", "key-abc12345", "llama") == 0.0
    # Sibling model remains usable
    assert rl.headroom("groq", "key-abc12345", "mistral") > 0.0
    ok_sib, wait_sib = rl.check_and_consume("groq", "key-abc12345", "mistral", 1.0, 1.0)
    assert ok_sib is True
    assert wait_sib == 0.0


def test_load_clears_provider_wide_blocked_until(tmp_path, monkeypatch):
    """Legacy provider-wide Retry-After holds are dropped on load."""
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    pw = AdaptiveRateLimiter._group_key("opencode", "key-NsmiAsLz", None)
    mg = AdaptiveRateLimiter._group_key("opencode", "key-NsmiAsLz", "deepseek")
    doc = {
        "version": 1,
        "groups": {
            pw: {"RPM": {"cap": 10.0, "tokens": 10.0, "last_refill": now},
                 "blocked_until": now + 3600},
            mg: {"RPM": {"cap": 10.0, "tokens": 10.0, "last_refill": now},
                 "blocked_until": now + 3600},
        },
    }
    (Path(tmp_path) / "rate_limits_state.json").write_text(json.dumps(doc))
    rl = make_limiter(tmp_path)
    rl.load()
    assert rl.get_group("opencode", "key-NsmiAsLz", None).blocked_until <= now
    assert rl.get_group("opencode", "key-NsmiAsLz", "deepseek").blocked_until == pytest.approx(now + 3600)


def test_snapshot_includes_blocked_until(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    now = 1_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 50.0)
    rl.on_429("groq", "key-abc12345", "llama", {"Retry-After": "15"})
    snap = rl.snapshot("groq", "key-abc12345", "llama")
    assert snap.get("blocked_until") == pytest.approx(now + 15.0)


def test_update_from_headers_model_only(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    pw_rpm_before = pw.buckets["RPM"].cap
    pw_tok_before = pw.buckets["RPM"].tokens
    rl.update_from_headers("groq", "key-abc12345", "llama", {
        "x-ratelimit-limit-requests": "100",
        "x-ratelimit-remaining-requests": "40",
    })
    assert mg.buckets["RPM"].cap == pytest.approx(100.0)
    assert mg.buckets["RPM"].tokens == pytest.approx(40.0)
    assert pw.buckets["RPM"].cap == pytest.approx(pw_rpm_before)
    assert pw.buckets["RPM"].tokens == pytest.approx(pw_tok_before)


def test_on_429_surprise_extra_soft_cut_when_model_headroom_high(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 10.0)
    pw = rl.get_group("groq", "key-abc12345", None)
    # Restore PW tokens/caps to a known full state for a clean cut measurement
    for b in pw.buckets.values():
        b.cap = 100.0
        b.tokens = 100.0
        b._period_consumed = 0.0
    cap_before = pw.buckets["RPM"].cap
    # High pre-attempt model headroom → surprise path
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=1.0)
    # Normal soft tick (M window ×0.95), then surprise soft tick → ×0.95 again
    assert pw.buckets["RPM"].cap == pytest.approx(round(cap_before * 0.95 * 0.95))


def test_on_429_no_surprise_when_model_headroom_low(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 10.0)
    pw = rl.get_group("groq", "key-abc12345", None)
    for b in pw.buckets.values():
        b.cap = 100.0
        b.tokens = 100.0
        b._period_consumed = 0.0
    cap_before = pw.buckets["RPM"].cap
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=0.2)
    assert pw.buckets["RPM"].cap == pytest.approx(cap_before * 0.95)


def test_on_429_surprise_throttled_within_60s(tmp_path, monkeypatch):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("groq", "key-abc12345", "llama", 1.0, 10.0)
    pw = rl.get_group("groq", "key-abc12345", None)
    for b in pw.buckets.values():
        b.cap = 100.0
        b.tokens = 100.0
        b._period_consumed = 0.0
    t0 = 1_000_000.0
    monkeypatch.setattr(rate_limiter.time, "time", lambda: t0)
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=1.0)
    cap_after_first = pw.buckets["RPM"].cap
    for b in pw.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 0.0
    monkeypatch.setattr(rate_limiter.time, "time", lambda: t0 + 10.0)
    rl.on_429("groq", "key-abc12345", "llama", {}, model_headroom_before=1.0)
    # Second 429 within 60s: soft once only (no second surprise); RPx rounded
    assert pw.buckets["RPM"].cap == pytest.approx(round(cap_after_first * 0.95))


def test_on_429_asymmetric_cuts(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    for g in (pw, mg):
        g.buckets["RPM"].cap = 100.0
        g.buckets["RPM"].tokens = 20.0  # headroom 0.2 → ladder blames M
        g.buckets["RPM"]._period_consumed = 10.0
    rl.on_429("groq", "key-abc12345", "llama", {})
    assert mg.buckets["RPM"].cap == pytest.approx(8.0)    # 10 * 0.8
    # Soft PW tick: 5% cut on M-window RPM (window-scaled tiny tick)
    assert pw.buckets["RPM"].cap == pytest.approx(95.0)
    assert pw.blocked_until <= time.time()


def test_on_429_headers_apply_to_model_not_pw(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    pw_cap = pw.buckets["RPM"].cap
    for g in (pw, mg):
        g.buckets["RPM"]._period_consumed = 1.0  # low history → fractional cut path if used
    rl.on_429("groq", "key-abc12345", "llama", {
        "x-ratelimit-limit-requests": "200",
        "x-ratelimit-remaining-requests": "0",
        "Retry-After": "30",
    })
    assert mg.buckets["RPM"].cap == pytest.approx(200.0)
    assert mg.buckets["RPM"].tokens == pytest.approx(0.0)
    # PW must not take header caps; soft-tick instead
    assert pw.buckets["RPM"].cap != pytest.approx(200.0)
    assert pw.buckets["RPM"].cap == pytest.approx(pw_cap * 0.95)


def test_on_success_pw_nudges_faster(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("groq", "key-abc12345", None)
    mg = rl.get_group("groq", "key-abc12345", "llama")
    pw.buckets["RPM"].cap = 100.0
    mg.buckets["RPM"].cap = 100.0
    for _ in range(10):
        rl.on_success("groq", "key-abc12345", "llama", 1.0)
    assert pw.buckets["RPM"].cap == pytest.approx(100.0 * 1.08)
    assert mg.buckets["RPM"].cap == pytest.approx(100.0)  # needs 20


def test_new_provider_wide_caps_are_10x_model_defaults(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "nvidia/nemotron")
    assert "TPM" in pw.buckets and "TPM" in mg.buckets
    assert pw.buckets["TPM"].cap == pytest.approx(mg.buckets["TPM"].cap * 10.0)
    assert pw.buckets["RPM"].cap == pytest.approx(mg.buckets["RPM"].cap * 10.0)


def test_single_consume_model_headroom_drops_more_than_provider(tmp_path):
    rl = make_limiter(tmp_path)
    # Force creation of both groups at full tokens
    rl.get_group("openrouter", "key-abc12345", None)
    rl.get_group("openrouter", "key-abc12345", "m")
    ok, _ = rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 2000.0)
    assert ok is True
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    assert mg.headroom() < pw.headroom()
    # Same absolute TPM debit; PW cap is 10× so remaining fraction is higher
    assert pw.buckets["TPM"].cap == pytest.approx(mg.buckets["TPM"].cap * 10.0)


def test_load_does_not_remultiply_provider_caps(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    # Simulate a learned (non-×10) cap already persisted
    pw.buckets["TPM"].cap = 12345.0
    pw.buckets["TPM"].tokens = 12345.0
    rl.flush()
    rl2 = make_limiter(tmp_path)
    rl2.load()
    pw2 = rl2.get_group("openrouter", "key-abc12345", None)
    assert pw2.buckets["TPM"].cap == pytest.approx(12345.0)


def test_model_and_provider_caps_grow_via_success_nudges(tmp_path):
    rl = make_limiter(tmp_path)
    rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 1.0)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    pw_cap0 = pw.buckets["TPM"].cap
    mg_cap0 = mg.buckets["TPM"].cap
    # Provider streak default 10; model streak default 20
    for _ in range(20):
        rl.on_success("openrouter", "key-abc12345", "m", 1.0)
    assert pw.buckets["TPM"].cap > pw_cap0
    assert mg.buckets["TPM"].cap > mg_cap0


def _enable_csv(monkeypatch, tmp_path):
    path = tmp_path / "tbf.csv"
    monkeypatch.setenv("RATE_BUCKET_CSV_ENABLED", "1")
    monkeypatch.setenv("RATE_BUCKET_CSV", str(path))
    rate_limiter._bucket_csv = rate_limiter.BucketEventCsv.from_env()
    return path


def test_csv_nudge_emits_row(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    monkeypatch.setattr(rate_limiter, "RATE_LEARN_SUCCESS_STREAK", 2)
    monkeypatch.setattr(rate_limiter, "RATE_LEARN_SUCCESS_STREAK_PROVIDER", 2)
    key, model = "sk-testkey99", "llama-3"
    rl.check_and_consume("groq", key, model, 1.0, 10.0)
    rl.on_success("groq", key, model, 10.0)
    rl.on_success("groq", key, model, 10.0)
    rows = list(csv.DictReader(path.open()))
    assert any(r["event"] == "nudge" and r["bucket"] in ("RPM", "TPM", "RPD")
               for r in rows)
    assert all(r["key_hint"] == key[-8:] for r in rows)


def test_csv_hard_429_emits_cut(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    key, model = "sk-testkey99", "llama-3"
    rl.check_and_consume("groq", key, model, 1.0, 10.0)
    g = rl.get_group("groq", key, model)
    for b in g.buckets.values():
        b._period_consumed = 10.0
    rl.on_429("groq", key, model, {})
    rows = list(csv.DictReader(path.open()))
    assert any(r["event"] == "cut" and r["reason"] == "hard_429" for r in rows)


def test_csv_ensure_fits_lift(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    key, model = "sk-testkey99", "llama-3"
    ok, _ = rl.check_and_consume("mistral", key, model, 1.0, 71_621.0)
    assert ok is True
    rows = list(csv.DictReader(path.open()))
    assert any(r["event"] == "lift" and r["reason"] == "request_burst"
               and r["bucket"] == "TPM" for r in rows)


def test_csv_header_pin_skips_same_cap_refresh(monkeypatch, tmp_path):
    path = _enable_csv(monkeypatch, tmp_path)
    rl = make_limiter(tmp_path)
    key, model = "sk-testkey99", "llama-3"
    headers = {
        "x-ratelimit-limit-requests": "45",
        "x-ratelimit-remaining-requests": "20",
        "x-ratelimit-limit-tokens": "9000",
        "x-ratelimit-remaining-tokens": "5000",
    }
    rl.update_from_headers("groq", key, model, headers, observed_at=100.0)
    n1 = len(list(csv.DictReader(path.open())))
    assert n1 > 0
    headers2 = {
        "x-ratelimit-limit-requests": "45",
        "x-ratelimit-remaining-requests": "10",
        "x-ratelimit-limit-tokens": "9000",
        "x-ratelimit-remaining-tokens": "1000",
    }
    rl.update_from_headers("groq", key, model, headers2, observed_at=200.0)
    n2 = len(list(csv.DictReader(path.open())))
    assert n2 == n1


def test_csv_disabled_no_file_on_learn(monkeypatch, tmp_path):
    path = tmp_path / "should_not_exist.csv"
    monkeypatch.delenv("RATE_BUCKET_CSV_ENABLED", raising=False)
    monkeypatch.setenv("RATE_BUCKET_CSV", str(path))
    rate_limiter._bucket_csv = rate_limiter.BucketEventCsv.from_env()
    rl = make_limiter(tmp_path)
    rl.on_429("groq", "sk-testkey99", "llama-3", {})
    assert not path.exists()


def test_on_429_skips_high_headroom_tpm_when_rpm_binding():
    """RPM exhaustion must not poison TPM (Gemini-style headerless 429)."""
    g = BucketGroup(provider_name="gemini", caps={"RPM": 15.0, "TPM": 250_000.0})
    rpm, tpm = g.buckets["RPM"], g.buckets["TPM"]
    rpm.tokens = 0.0
    rpm._period_consumed = 10.0
    tpm.tokens = tpm.cap  # ~100% headroom
    tpm._period_consumed = 40_000.0
    tpm_before = tpm.cap
    g.on_429({})
    assert rpm.cap < 15.0
    assert tpm.cap == pytest.approx(tpm_before)


def test_on_429_cuts_lowest_headroom_when_none_binding():
    g = BucketGroup(provider_name="mistral", caps={"RPM": 10.0, "TPM": 1000.0})
    rpm, tpm = g.buckets["RPM"], g.buckets["TPM"]
    # Both above binding threshold; RPM clearly tighter so TPM is spared
    rpm.tokens = 4.0   # headroom 0.4
    tpm.tokens = 600.0  # headroom 0.6
    rpm._period_consumed = 1.0
    tpm._period_consumed = 100.0
    tpm_before = tpm.cap
    g.on_429({})
    assert rpm.cap == pytest.approx(5.0)  # hard half with thin history
    assert tpm.cap == pytest.approx(tpm_before)


def test_r_dimension_soft_floor_is_integer():
    b = TokenBucket(window_seconds=WINDOW, cap=15.0, tokens=15.0, dimension="R")
    b._floor_cap = 15.0
    b._period_consumed = 10.0
    b.on_429(observed_rate=1.0, soft=True)  # 1*0.95 < floor 7.5 → floor
    assert b.cap == 8
    assert isinstance(b.cap, (int, float))
    assert float(b.cap) == int(b.cap)


def test_r_dimension_soft_cut_rounds_to_integer():
    b = TokenBucket(window_seconds=WINDOW, cap=15.0, tokens=15.0, dimension="R")
    b._period_consumed = 1.0
    b.on_429(observed_rate=1.0, soft=True)  # 15 * 0.9 = 13.5 → 14
    assert b.cap == 14


def test_gemini_default_tpm_is_250k():
    assert PROVIDER_RATE_DEFAULTS["gemini"]["TPM"] == 250_000


def test_gemini_provider_wide_tpm_starts_at_2_5m(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("gemini", "key-abc12345", None)
    mg = rl.get_group("gemini", "key-abc12345", "gemini-2.5-flash-lite")
    assert mg.buckets["TPM"].cap == pytest.approx(250_000.0)
    assert pw.buckets["TPM"].cap == pytest.approx(2_500_000.0)


def test_list_groups_emits_integer_rpx(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("gemini", "key-abc12345", None)
    pw.buckets["RPM"].cap = 7.5
    pw.buckets["RPM"].tokens = 7.5
    gk = rl._group_key("gemini", "key-abc12345", None)
    rows = rl.list_groups(include_orphans=True, configured_ids={gk})
    assert len(rows) == 1
    rpm = rows[0]["buckets"]["RPM"]
    assert rpm["cap"] == 8
    assert isinstance(rpm["cap"], int)
    assert isinstance(rpm["used"], int)
    assert isinstance(rpm["tokens"], int)


FULL_LIMIT_KEYS = {
    "RPM", "RPH", "RPD", "RPW", "RPMo",
    "TPM", "TPH", "TPD", "TPW", "TPMo",
}


def test_load_caps_for_returns_full_grid():
    from rate_limiter import _load_caps_for, WINDOWS
    caps = _load_caps_for("openrouter")
    assert set(caps) == FULL_LIMIT_KEYS
    assert caps["TPH"] == pytest.approx(caps["TPM"] * (WINDOWS["H"] / WINDOWS["M"]))
    assert caps["TPMo"] == pytest.approx(caps["TPM"] * (WINDOWS["Mo"] / WINDOWS["M"]))
    assert caps["TPMo"] >= caps["TPW"] >= caps["TPD"] >= caps["TPH"] >= caps["TPM"]
    assert caps["RPMo"] >= caps["RPW"] >= caps["RPD"] >= caps["RPH"] >= caps["RPM"]


def test_explicit_rpd_overrides_linear_scale():
    from rate_limiter import _load_caps_for, WINDOWS
    caps = _load_caps_for("gemini")
    assert caps["RPD"] == pytest.approx(1500.0)
    # Must not be forced up to RPM * (day/minute)
    assert caps["RPD"] < caps["RPM"] * (WINDOWS["D"] / WINDOWS["M"])
    assert caps["RPD"] >= caps["RPH"] >= caps["RPM"]


def test_auth_rpd_override_reclamps_scaled_rph(tmp_path):
    from rate_limiter import _load_caps_for
    auth_file = Path(tmp_path) / "auth.json"
    auth_file.write_text(json.dumps({
        "rate_defaults": {"openrouter": {"RPD": 100}},
    }))
    rl = AdaptiveRateLimiter(
        state_file=Path(tmp_path) / "rate_limits_state.json",
        auth_file=auth_file,
    )
    caps = rl._caps_for("openrouter", provider_wide=False)
    linear_rph = _load_caps_for("openrouter")["RPH"]
    assert linear_rph > caps["RPD"]
    assert caps["RPD"] == pytest.approx(100.0)
    assert caps["RPH"] == pytest.approx(100.0)
    assert caps["RPD"] >= caps["RPH"] >= caps["RPM"]


def test_to_dict_persists_all_ten_buckets():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    d = g.to_dict()
    assert FULL_LIMIT_KEYS <= set(k for k, v in d.items() if isinstance(v, dict))


def test_load_backfills_missing_windows(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    gk = rl._group_key("openrouter", "key-abc12345", None)
    rl._groups[gk].buckets = {
        "RPM": pw.buckets["RPM"],
        "TPM": pw.buckets["TPM"],
    }
    learned_tpm = 12345.0
    rl._groups[gk].buckets["TPM"].cap = learned_tpm
    rl.flush()
    rl2 = make_limiter(tmp_path)
    rl2.load()
    pw2 = rl2.get_group("openrouter", "key-abc12345", None)
    assert set(pw2.buckets) == FULL_LIMIT_KEYS
    assert pw2.buckets["TPM"].cap == pytest.approx(learned_tpm)
    assert "TPMo" in pw2.buckets


def test_new_groups_always_have_ten_buckets(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    assert set(pw.buckets) == FULL_LIMIT_KEYS
    assert set(mg.buckets) == FULL_LIMIT_KEYS
    assert pw.buckets["TPM"].cap == pytest.approx(mg.buckets["TPM"].cap * 10.0)
    assert pw.buckets["TPMo"].cap == pytest.approx(mg.buckets["TPMo"].cap * 10.0)


def test_one_consume_debits_all_ten_and_mo_pct_drops_less(tmp_path):
    rl = make_limiter(tmp_path)
    rl.get_group("openrouter", "key-abc12345", "m")
    mg = rl.get_group("openrouter", "key-abc12345", "m")
    for b in mg.buckets.values():
        b.tokens = b.cap
    tpm_before = mg.buckets["TPM"].tokens / mg.buckets["TPM"].cap
    tpmo_before = mg.buckets["TPMo"].tokens / mg.buckets["TPMo"].cap
    ok, _ = rl.check_and_consume("openrouter", "key-abc12345", "m", 1.0, 500.0)
    assert ok is True
    tpm_after = mg.buckets["TPM"].tokens / mg.buckets["TPM"].cap
    tpmo_after = mg.buckets["TPMo"].tokens / mg.buckets["TPMo"].cap
    assert (tpm_before - tpm_after) > (tpmo_before - tpmo_after)


def test_soft_429_ticks_all_buckets_including_high_headroom():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    g.buckets["TPM"].tokens = 0.0                     # binding-looking
    g.buckets["TPMo"].tokens = g.buckets["TPMo"].cap  # full
    tpm0 = g.buckets["TPM"].cap
    tpmo0 = g.buckets["TPMo"].cap
    g.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)
    assert g.buckets["TPM"].cap < tpm0
    assert g.buckets["TPMo"].cap < tpmo0  # must tick even when not binding
    assert (tpm0 - g.buckets["TPM"].cap) / tpm0 > (tpmo0 - g.buckets["TPMo"].cap) / tpmo0


def test_soft_tick_keeps_full_long_window_tokens():
    """One PW soft tick must not empty full long windows / collapse headroom."""
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    for b in g.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 0.0
    tpm0 = g.buckets["TPM"].cap
    tpmo0 = g.buckets["TPMo"].cap
    tpmo_tokens0 = g.buckets["TPMo"].tokens
    hr0 = g.headroom()
    assert hr0 == pytest.approx(1.0)

    g.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)

    assert g.buckets["TPM"].cap < tpm0
    assert g.buckets["TPMo"].cap < tpmo0
    # Soft tick clamps tokens to new_cap; does not force tokens=0.
    assert g.buckets["TPMo"].tokens == pytest.approx(g.buckets["TPMo"].cap)
    assert g.buckets["TPMo"].tokens / tpmo_tokens0 > 0.99
    # Group headroom stays near-full; must not collapse solely from the tick.
    assert g.headroom() > 0.9


def test_pw_soft_tick_via_limiter_no_retry_after(tmp_path):
    rl = make_limiter(tmp_path)
    pw = rl.get_group("openrouter", "key-abc12345", None)
    rl.get_group("openrouter", "key-abc12345", "m")
    for b in pw.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 0.0
    tpm0, tpmo0 = pw.buckets["TPM"].cap, pw.buckets["TPMo"].cap
    rl.on_429("openrouter", "key-abc12345", "m", {}, model_headroom_before=0.5)
    assert pw.buckets["TPM"].cap < tpm0
    assert (tpm0 - pw.buckets["TPM"].cap) / tpm0 > (tpmo0 - pw.buckets["TPMo"].cap) / tpmo0
    assert pw.blocked_until <= time.time()


def test_many_soft_ticks_eventually_move_mo():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    before = g.buckets["TPH"].cap
    for _ in range(200):
        g.on_429({}, apply_retry_after=False, apply_headers=False, soft=True)
    assert g.buckets["TPH"].cap < before * 0.99


def test_on_success_nudges_only_minute_windows():
    from rate_limiter import BucketGroup, _load_caps_for, RATE_LEARN_SUCCESS_STREAK
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    for b in g.buckets.values():
        b._header_pinned = False
        b._consecutive_successes = RATE_LEARN_SUCCESS_STREAK - 1
    before = {n: b.cap for n, b in g.buckets.items()}
    changes = g.on_success(100.0)
    nudged = {n for n, c in changes if c.event == "nudge"}
    assert nudged <= {"RPM", "TPM"}
    assert "TPM" in nudged or "RPM" in nudged
    # Long windows do not success-nudge at the minute streak; clamp may lift them
    # to preserve Cap(long) ≥ Cap(short) after an M nudge.
    for n, c in changes:
        if n not in ("RPM", "TPM"):
            assert c.event == "clamp"


def test_on_success_long_window_nudges_after_long_streak():
    from rate_limiter import (
        BucketGroup, _load_caps_for, RATE_LEARN_LONG_STREAK, RATE_LEARN_LONG_NUDGE_PCT,
    )
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    for b in g.buckets.values():
        b._header_pinned = False
        b._consecutive_successes = RATE_LEARN_LONG_STREAK - 1
    before = g.buckets["TPH"].cap
    changes = g.on_success(100.0)
    nudged = {n for n, c in changes if c.event == "nudge"}
    assert "TPH" in nudged or "TPM" in nudged
    assert g.buckets["TPH"].cap >= before


def test_ladder_429_clear_m_cuts_hour_only():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    for b in g.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 5.0
    # M clearly free; leave H as the blamed long window via ladder (all clear → Mo
    # — set H slightly lower headroom among long so R/T pick prefers H if we
    # only clear M... with all clear blame is Mo. Force M clear and H not clear.
    g.buckets["RPM"].tokens = g.buckets["RPM"].cap
    g.buckets["TPM"].tokens = g.buckets["TPM"].cap
    g.buckets["RPH"].tokens = 0.0
    g.buckets["TPH"].tokens = g.buckets["TPH"].cap
    before = {n: b.cap for n, b in g.buckets.items()}
    changes = g.on_429({})
    cut = {n for n, c in changes if c.event == "cut"}
    assert cut == {"RPH"}
    assert g.buckets["RPM"].cap == pytest.approx(before["RPM"])
    assert g.buckets["TPM"].cap == pytest.approx(before["TPM"])
    assert g.buckets["RPH"].cap < before["RPH"]


def test_ladder_429_low_m_cuts_minute_only():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    for b in g.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 5.0
    g.buckets["RPM"].tokens = 0.0
    before = {n: b.cap for n, b in g.buckets.items()}
    changes = g.on_429({})
    cut = {n for n, c in changes if c.event == "cut"}
    assert cut == {"RPM"}
    assert g.buckets["RPH"].cap <= before["RPH"]  # may clamp-pull with RPM cut


def test_reclamp_upper_bounds_long_from_short():
    from rate_limiter import BucketGroup, reclamp_bucket_caps
    g = BucketGroup(provider_name="openrouter", caps={"TPM": 1000.0, "TPH": 1_000_000.0})
    # Force illegal upper: TPH >> TPM*60
    g.buckets["TPM"].cap = 1000.0
    g.buckets["TPH"].cap = 1_000_000.0
    reclamp_bucket_caps(g.buckets)
    assert g.buckets["TPH"].cap <= g.buckets["TPM"].cap * 60 + 1e-6


def test_reclamp_order_raise_long_grants_opened_capacity():
    """Hard 429 zeros Mo then reclamp raises Mo back to W — must not sticky-empty Mo."""
    from rate_limiter import BucketGroup, reclamp_bucket_caps, _load_caps_for
    g = BucketGroup(provider_name="opencode", caps=_load_caps_for("opencode"))
    # Simulate post-hard-429: Mo halved+zeroed while W still healthy/full.
    g.buckets["RPW"].cap = 100_800.0
    g.buckets["RPW"].tokens = 100_800.0
    g.buckets["RPMo"].cap = 50_400.0
    g.buckets["RPMo"].tokens = 0.0
    reclamp_bucket_caps(g.buckets)
    assert g.buckets["RPMo"].cap == pytest.approx(100_800.0)
    assert g.buckets["RPMo"].tokens == pytest.approx(50_400.0)
    assert g.buckets["RPMo"].headroom() == pytest.approx(0.5, abs=0.01)


def test_headerless_429_does_not_sticky_empty_mo():
    """When shorter windows are clear, ladder blames Mo; reclamp must not leave Mo at 0%."""
    from rate_limiter import BucketGroup, _load_caps_for
    caps = _load_caps_for("opencode")
    g = BucketGroup(provider_name="opencode", caps=caps)
    for b in g.buckets.values():
        b.tokens = b.cap
        b._period_consumed = 0.0
    before_mo = g.buckets["RPMo"].cap
    g.on_429({})  # headerless → ladder → hard cut → reclamp
    # Cap should be restored to at least the weekly floor; tokens must recover with it.
    assert g.buckets["RPMo"].cap >= g.buckets["RPW"].cap - 1e-6
    assert g.buckets["RPMo"].tokens > 0.0
    assert g.buckets["RPMo"].headroom() >= 0.4
    # And we must not have permanently collapsed below the pre-cut Mo when W held.
    assert g.buckets["RPMo"].cap >= min(before_mo, g.buckets["RPW"].cap) - 1e-6


def test_force_consume_admits_when_empty():
    from rate_limiter import BucketGroup, _load_caps_for
    g = BucketGroup(provider_name="openrouter", caps=_load_caps_for("openrouter"))
    g.buckets["RPM"].tokens = 0.0
    ok, wait, _ = g.consume(1.0, 10.0, force=True)
    assert ok is True
    assert wait == 0.0


def test_format_bucket_used_shows_gross_when_release_zeros_net():
    """Cascaded consume+release pairs net to zero; dashboard still shows gross spend."""
    from rate_limiter import TokenBucket, _format_bucket_metrics
    b = TokenBucket(window_seconds=86400.0, cap=1500.0, tokens=882.0, dimension="R")
    for _ in range(34):
        b.record_usage(1.0)
        b.record_usage(-1.0)
    assert b.usage_in_window() == 0.0
    assert _format_bucket_metrics("RPD", b)["used"] == 34


def test_format_bucket_used_survives_refill():
    """Dashboard used is sliding-window spend; continuous refill must not zero it."""
    from rate_limiter import TokenBucket, _format_bucket_metrics
    import time as _time
    b = TokenBucket(window_seconds=3600.0, cap=15_000_000.0, tokens=15_000_000.0, dimension="T")
    b.tokens -= 12.0
    b.record_usage(12.0)
    b.refill(_time.time() + 1.0)  # refill would clear fill-deficit instantly at this rate
    # Simulate full refill of the continuous bucket
    b.tokens = b.cap
    m = _format_bucket_metrics("TPH", b)
    assert m["used"] == pytest.approx(12.0)


def test_header_map_accepts_minute_aliases():
    from rate_limiter import BucketGroup, _format_bucket_metrics
    g = BucketGroup(provider_name="cerebras", caps={"RPM": 10.0, "TPM": 100_000.0,
                                                    "RPH": 150.0, "TPH": 1_000_000.0})
    for b in g.buckets.values():
        b.tokens = b.cap
    g.update_from_headers({
        "x-ratelimit-limit-requests-minute": "10",
        "x-ratelimit-remaining-requests-minute": "7",
        "x-ratelimit-limit-tokens-minute": "100000",
        "x-ratelimit-remaining-tokens-minute": "90000",
    })
    assert g.buckets["RPM"].tokens == pytest.approx(7.0)
    assert g.buckets["TPM"].tokens == pytest.approx(90_000.0)
    assert _format_bucket_metrics("RPM", g.buckets["RPM"])["used"] == 3
    assert _format_bucket_metrics("TPM", g.buckets["TPM"])["used"] == pytest.approx(10_000.0)


def test_header_remaining_does_not_wipe_local_window_usage():
    from rate_limiter import TokenBucket, _format_bucket_metrics
    b = TokenBucket(window_seconds=3600.0, cap=1_000_000.0, tokens=1_000_000.0, dimension="T")
    b.record_usage(50_000.0)
    # Upstream hour remaining implies only 8k used — must not reset local 50k.
    b.set_from_header(cap=1_000_000.0, remaining=992_000.0)
    assert _format_bucket_metrics("TPH", b)["used"] == pytest.approx(50_000.0)
    # Upstream reporting more spend tops up.
    b.set_from_header(cap=1_000_000.0, remaining=900_000.0, observed_at=time.time() + 1)
    assert _format_bucket_metrics("TPH", b)["used"] == pytest.approx(100_000.0)


def test_usage_events_persist_roundtrip():
    from rate_limiter import BucketGroup
    g = BucketGroup(provider_name="groq", caps={"RPM": 30.0, "TPM": 6000.0})
    g.consume(1.0, 100.0)
    d = g.to_dict()
    assert "usage_events" in d["TPM"]
    g2 = BucketGroup.from_dict(d, provider_name="groq")
    assert g2.buckets["TPM"].usage_in_window() == pytest.approx(100.0)
    assert g2.buckets["RPM"].usage_in_window() == pytest.approx(1.0)


def test_header_missing_remaining_preserves_usage():
    from rate_limiter import BucketGroup, _format_bucket_metrics
    g = BucketGroup(provider_name="cerebras", caps={"TPM": 100_000.0, "TPH": 1_000_000.0})
    g.consume(0.0, 12_000.0)
    before = _format_bucket_metrics("TPH", g.buckets["TPH"])["used"]
    g.update_from_headers({
        "x-ratelimit-limit-tokens-hour": "1000000",
        # no remaining header
    })
    assert _format_bucket_metrics("TPH", g.buckets["TPH"])["used"] == pytest.approx(before)


# ── Comparable headroom (cap-scaled binding window) ───────────────────────────

def test_bucketgroup_comparable_prefers_absolute_remaining():
    tiny = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 100_000.0})
    tiny.buckets["RPM"].tokens = 9.0
    tiny.buckets["RPM"].last_refill = time.time()
    tiny.buckets["TPM"].tokens = 100_000.0
    tiny.buckets["TPM"].last_refill = time.time()
    large = BucketGroup(provider_name="p", caps={"RPM": 100.0, "TPM": 100_000.0})
    large.buckets["RPM"].tokens = 20.0
    large.buckets["RPM"].last_refill = time.time()
    large.buckets["TPM"].tokens = 100_000.0
    large.buckets["TPM"].last_refill = time.time()
    peer_caps = {"RPM": 100.0, "TPM": 100_000.0}
    # tiny: RPM binds at 90% → 9/100 = 0.09; large: RPM binds at 20% → 20/100 = 0.20
    assert tiny.headroom() > large.headroom()
    assert tiny.comparable_headroom(peer_caps) < large.comparable_headroom(peer_caps)


def test_comparable_rpm_bound_not_crushed_by_peer_tpm():
    g = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 100.0})
    g.buckets["RPM"].tokens = 5.0
    g.buckets["RPM"].last_refill = time.time()
    g.buckets["TPM"].tokens = 90.0
    g.buckets["TPM"].last_refill = time.time()
    peer_caps = {"RPM": 10.0, "TPM": 250_000.0}
    # RPM binds (50% < 90%); score = 5/10, not 90/250000
    assert g.comparable_headroom(peer_caps) == pytest.approx(0.5, abs=0.01)


def test_comparable_blocked_is_zero():
    g = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 1000.0})
    g.blocked_until = time.time() + 60
    assert g.comparable_headroom({"RPM": 10.0, "TPM": 1000.0}) == 0.0


def test_comparable_empty_peer_caps_falls_back_to_raw():
    g = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 1000.0})
    g.buckets["RPM"].tokens = 5.0
    g.buckets["RPM"].last_refill = time.time()
    g.buckets["TPM"].tokens = 1000.0
    g.buckets["TPM"].last_refill = time.time()
    assert g.comparable_headroom({}) == pytest.approx(0.5, abs=0.01)


def test_comparable_missing_group_is_one(tmp_path):
    rl = make_limiter(tmp_path)
    assert rl.comparable_headroom("gemini", "sk-testkeyxx", "gemini-flash") == 1.0
    assert rl.rank_comparable_headroom("gemini", "sk-testkeyxx", "gemini-flash") == 1.0


def test_limiter_comparable_and_rank(tmp_path):
    rl = make_limiter(tmp_path)
    key = "key-abc12345"
    # Tiny model group: high raw %, low absolute RPM remaining
    tiny_gk = AdaptiveRateLimiter._group_key("p", key, "tiny")
    tiny = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 100_000.0})
    tiny.buckets["RPM"].tokens = 9.0
    tiny.buckets["RPM"].last_refill = time.time()
    tiny.buckets["TPM"].tokens = 100_000.0
    tiny.buckets["TPM"].last_refill = time.time()
    # Large model group
    large_gk = AdaptiveRateLimiter._group_key("p", key, "large")
    large = BucketGroup(provider_name="p", caps={"RPM": 100.0, "TPM": 100_000.0})
    large.buckets["RPM"].tokens = 20.0
    large.buckets["RPM"].last_refill = time.time()
    large.buckets["TPM"].tokens = 100_000.0
    large.buckets["TPM"].last_refill = time.time()
    # Inflated PW group should not enter peer scale (model-scope only)
    pw_gk = AdaptiveRateLimiter._group_key("p", key, None)
    pw = BucketGroup(provider_name="p", caps={"RPM": 1000.0, "TPM": 1_000_000.0})
    for b in pw.buckets.values():
        b.tokens = b.cap
        b.last_refill = time.time()
    with rl._lock:
        rl._groups[tiny_gk] = tiny
        rl._groups[large_gk] = large
        rl._groups[pw_gk] = pw
    assert rl.comparable_headroom("p", key, "tiny") < rl.comparable_headroom("p", key, "large")
    # PW full → rank for large is model comparable (min of full PW and model)
    assert rl.rank_comparable_headroom("p", key, "large") == pytest.approx(
        rl.comparable_headroom("p", key, "large"), abs=0.01
    )
    # PW near empty should pull rank down
    pw.buckets["RPM"].tokens = 1.0
    pw.buckets["RPM"].last_refill = time.time()
    assert rl.rank_comparable_headroom("p", key, "large") < rl.comparable_headroom("p", key, "large")


def test_list_groups_includes_comparable_headroom(tmp_path):
    rl = make_limiter(tmp_path)
    key = "key-abc12345"
    gk = AdaptiveRateLimiter._group_key("p", key, "m")
    g = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 1000.0})
    g.buckets["RPM"].tokens = 5.0
    g.buckets["RPM"].last_refill = time.time()
    g.buckets["TPM"].tokens = 1000.0
    g.buckets["TPM"].last_refill = time.time()
    with rl._lock:
        rl._groups[gk] = g
    rows = rl.list_groups(include_orphans=True)
    row = next(r for r in rows if r["id"] == gk)
    assert "comparable_headroom" in row
    assert row["comparable_headroom"] == pytest.approx(0.5, abs=0.01)


def test_peer_caps_median_ignores_outlier_max(tmp_path):
    """One absurd-cap model must not collapse a full normal peer's comparable score."""
    rl = make_limiter(tmp_path)
    key = "key-abc12345"
    now = time.time()

    def _full_group(rpm: float, tpm: float) -> BucketGroup:
        g = BucketGroup(provider_name="p", caps={"RPM": rpm, "TPM": tpm})
        for b in g.buckets.values():
            b.tokens = b.cap
            b.last_refill = now
        return g

    normals = []
    for i, rpm in enumerate((10.0, 12.0, 15.0, 20.0, 25.0)):
        gk = AdaptiveRateLimiter._group_key("p", key, f"n{i}")
        g = _full_group(rpm, 100_000.0)
        normals.append((gk, g, f"n{i}"))
    huge_gk = AdaptiveRateLimiter._group_key("p", key, "huge")
    huge = _full_group(2e9, 4e16)
    with rl._lock:
        for gk, g, _ in normals:
            rl._groups[gk] = g
        rl._groups[huge_gk] = huge
    # Median RPM among 6 groups: sorted 10,12,15,20,25,2e9 → (15+20)/2 = 17.5
    # Full normal at RPM=20 → comparable = 20/17.5 > 1 → clamp 1.0
    assert rl.comparable_headroom("p", key, "n3") == pytest.approx(1.0, abs=0.01)
    # Under max-scale this would be ~20/2e9 ≈ 0; ensure we stayed usable
    assert rl.comparable_headroom("p", key, "n3") > 0.05


def test_peer_caps_even_n_median_averages_central(tmp_path):
    rl = make_limiter(tmp_path)
    key = "key-abc12345"
    now = time.time()
    for name, rpm in (("a", 10.0), ("b", 30.0)):
        gk = AdaptiveRateLimiter._group_key("p", key, name)
        g = BucketGroup(provider_name="p", caps={"RPM": rpm, "TPM": 1000.0})
        g.buckets["RPM"].tokens = rpm
        g.buckets["RPM"].last_refill = now
        g.buckets["TPM"].tokens = 1000.0
        g.buckets["TPM"].last_refill = now
        with rl._lock:
            rl._groups[gk] = g
    with rl._lock:
        peer = rl._pool_peer_caps_unlocked()
    assert peer["RPM"] == pytest.approx(20.0, abs=0.01)


def test_raw_headroom_unchanged_by_peer_scale(tmp_path):
    rl = make_limiter(tmp_path)
    key = "key-abc12345"
    gk = AdaptiveRateLimiter._group_key("p", key, "m")
    g = BucketGroup(provider_name="p", caps={"RPM": 10.0, "TPM": 1000.0})
    g.buckets["RPM"].tokens = 5.0
    g.buckets["RPM"].last_refill = time.time()
    g.buckets["TPM"].tokens = 1000.0
    g.buckets["TPM"].last_refill = time.time()
    with rl._lock:
        rl._groups[gk] = g
    assert rl.headroom("p", key, "m") == pytest.approx(0.5, abs=0.01)
    assert rl.model_headroom("p", key, "m") == pytest.approx(0.5, abs=0.01)
