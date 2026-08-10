"""HTTP tests for GET /v1/capacity."""
import router


def test_capacity_requires_auth():
    client = router.app.test_client()
    r = client.get("/v1/capacity")
    assert r.status_code in (401, 403)


def test_capacity_ok_shape(monkeypatch):
    monkeypatch.setattr(router, "_auth_check", lambda: None)
    monkeypatch.setattr(
        router, "_capacity_candidates",
        lambda: [
            {"headroom": 0.8, "health_bucket": 0, "breaker_open": False, "blocked": False},
            {"headroom": 0.7, "health_bucket": 0, "breaker_open": False, "blocked": False},
            {"headroom": 0.6, "health_bucket": 0, "breaker_open": False, "blocked": False},
        ],
    )
    client = router.app.test_client()
    r = client.get("/v1/capacity")
    assert r.status_code == 200
    body = r.get_json()
    for key in ("generated_at", "capacity", "advice", "interval_multiplier",
                "skip", "reasons", "components"):
        assert key in body
    assert body["advice"] == "fast"
    assert body["skip"] is False
    assert isinstance(body["reasons"], list)
    assert "top_k" in body["components"]


def test_model_headroom_missing_group_is_full(tmp_path):
    from rate_limiter import AdaptiveRateLimiter
    lim = AdaptiveRateLimiter(state_file=tmp_path / "rl.json")
    assert lim.model_headroom("gemini", "sk-testkeyxx", "gemini-flash") == 1.0
    assert lim.model_blocked_until("gemini", "sk-testkeyxx", "gemini-flash") is None
