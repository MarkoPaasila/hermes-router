import time, pytest
from rate_limiter import TokenBucket

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
