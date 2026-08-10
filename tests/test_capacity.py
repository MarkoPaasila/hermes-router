"""Tests for capacity.py pool scoring and advice mapping."""
import pytest

from capacity import advice_for, score_pool


def test_advice_thresholds():
    assert advice_for(0.60) == ("fast", 0.5, False)
    assert advice_for(0.99) == ("fast", 0.5, False)
    assert advice_for(0.35) == ("normal", 1.0, False)
    assert advice_for(0.59) == ("normal", 1.0, False)
    assert advice_for(0.15) == ("slow", 2.0, False)
    assert advice_for(0.34) == ("slow", 2.0, False)
    assert advice_for(0.149) == ("skip", 4.0, True)
    assert advice_for(0.0) == ("skip", 4.0, True)


def test_no_candidates_skips():
    out = score_pool([])
    assert out["capacity"] == 0.0
    assert out["advice"] == "skip"
    assert out["skip"] is True
    assert out["interval_multiplier"] == 4.0
    assert "no_candidates" in out["reasons"]
    assert out["components"]["usable_candidates"] == 0


def _c(headroom, health_bucket=0, breaker_open=False, blocked=False):
    return {
        "headroom": headroom,
        "health_bucket": health_bucket,
        "breaker_open": breaker_open,
        "blocked": blocked,
    }


def test_top_k_mean_of_best_three():
    # effectives: 1.0, 0.9, 0.8, 0.1 → top3 mean = 0.9
    out = score_pool([
        _c(1.0), _c(0.9), _c(0.8), _c(0.1),
    ])
    assert out["capacity"] == pytest.approx(0.9)
    assert out["advice"] == "fast"
    assert out["skip"] is False
    assert out["components"]["top_k"] == 3
    assert out["components"]["usable_candidates"] == 4


def test_breaker_zeros_effective():
    out = score_pool([
        _c(1.0, breaker_open=True),
        _c(1.0, breaker_open=True),
        _c(0.5),
    ])
    # only one usable (>0.05) → exhausted skip path
    assert out["skip"] is True
    assert out["advice"] == "skip"


def test_blocked_zeros_effective():
    out = score_pool([
        _c(1.0, blocked=True),
        _c(1.0, blocked=True),
        _c(0.4),
    ])
    assert out["skip"] is True


def test_health_factor_degraded():
    # headroom 1.0 × 0.7 = 0.7 → still fast; two more healthy full
    out = score_pool([
        _c(1.0, health_bucket=1),
        _c(1.0),
        _c(1.0),
    ])
    # top3: 1.0, 1.0, 0.7 → mean 0.9
    assert out["capacity"] == pytest.approx(0.9)
    assert out["components"]["health"] == pytest.approx(0.9)  # mean health factors of top-k


def test_health_bucket_bad():
    out = score_pool([_c(1.0, health_bucket=2), _c(1.0, health_bucket=2), _c(1.0, health_bucket=2)])
    # 0.3 each → capacity 0.3 → slow
    assert out["capacity"] == pytest.approx(0.3)
    assert out["advice"] == "slow"
    assert out["interval_multiplier"] == 2.0


def test_thin_usable_set_skips_even_if_mean_high():
    # one strong candidate, rest dead → <2 usable
    out = score_pool([_c(1.0), _c(0.0), _c(0.0)])
    assert out["skip"] is True
    assert out["advice"] == "skip"


def test_response_shape_keys():
    out = score_pool([_c(0.5), _c(0.5), _c(0.5)])
    for key in ("capacity", "advice", "interval_multiplier", "skip", "reasons", "components"):
        assert key in out
    for key in ("headroom", "health", "usable_candidates", "top_k"):
        assert key in out["components"]
