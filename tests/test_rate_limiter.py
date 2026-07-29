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

def test_restore_clamps():
    b = make_bucket(cap=10.0, tokens=9.0)
    b.restore(5.0)
    assert b.tokens == 10.0

def test_inactive_after_quiet_period():
    # Day-window bucket can go inactive when quiet; minute windows cannot.
    b = TokenBucket(window_seconds=86400.0, cap=100.0, tokens=100.0)
    b.check_inactive(activity=2)   # < max(10, 100*0.1)=10
    assert b.active is False

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


import json
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
    # Day-window buckets go inactive when quiet; minute buckets stay active.
    assert any(not b.active for b in g.buckets.values())
    assert all(b.active for b in g.buckets.values() if b.window_seconds <= 60)

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
