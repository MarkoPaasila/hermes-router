"""Tests for unsuitable-model classification and exponential backoff cooldown."""
import pytest

import router


def test_404_is_always_unsuitable():
    assert router._is_unsuitable_model_error(404, "") is True
    assert router._is_unsuitable_model_error(404, "[{") is True


def test_400_model_not_found_is_unsuitable():
    for body in (
        "model not found",
        "Unknown model: foo",
        "The model `bar` does not exist",
        "model is not supported for this endpoint",
        '{"error":{"message":"NOT_FOUND: model xyz"}}',
    ):
        assert router._is_unsuitable_model_error(400, body) is True, body


def test_400_payload_shaped_is_not_unsuitable():
    body = (
        'Error from provider (DeepSeek): The `reasoning_content` in the '
        'thinking mode must be passed back to the API.'
    )
    assert router._is_unsuitable_model_error(400, body) is False


def test_429_is_not_unsuitable():
    assert router._is_unsuitable_model_error(429, "rate limit") is False


def test_unsuitable_backoff_exponential(monkeypatch):
    store = router.UnsuitableModelCooldown(base_s=60.0, cap_s=3600.0)
    now = [1_000_000.0]
    monkeypatch.setattr(store, "_now", lambda: now[0])

    assert store.is_cooling("gemini", "imagen-4.0") is False

    store.record("gemini", "imagen-4.0")
    assert store.is_cooling("gemini", "imagen-4.0") is True
    assert store.ready_in("gemini", "imagen-4.0") == pytest.approx(60.0)
    assert store.failures("gemini", "imagen-4.0") == 1

    now[0] += 61
    assert store.is_cooling("gemini", "imagen-4.0") is False

    store.record("gemini", "imagen-4.0")
    assert store.failures("gemini", "imagen-4.0") == 2
    assert store.ready_in("gemini", "imagen-4.0") == pytest.approx(120.0)

    store.record("gemini", "imagen-4.0")
    assert store.failures("gemini", "imagen-4.0") == 3
    assert store.ready_in("gemini", "imagen-4.0") == pytest.approx(240.0)


def test_unsuitable_backoff_caps(monkeypatch):
    store = router.UnsuitableModelCooldown(base_s=60.0, cap_s=3600.0)
    now = [0.0]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    for _ in range(19):
        store.record("p", "m")
        now[0] += store.ready_in("p", "m") + 0.1
    store.record("p", "m")
    assert store.ready_in("p", "m") == pytest.approx(3600.0)


def test_unsuitable_clear_on_success(monkeypatch):
    store = router.UnsuitableModelCooldown(base_s=60.0, cap_s=3600.0)
    now = [0.0]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    store.record("gemini", "flash")
    assert store.is_cooling("gemini", "flash") is True
    store.clear("gemini", "flash")
    assert store.is_cooling("gemini", "flash") is False
    assert store.failures("gemini", "flash") == 0


def test_route_skips_cooling_unsuitable_model(monkeypatch):
    """While a model is cooling, _route_completion must not call forward for it."""
    store = router.UnsuitableModelCooldown(base_s=60.0, cap_s=3600.0)
    store.record("gemini", "bad-model")
    monkeypatch.setattr(router, "unsuitable_models", store)

    provider = {
        "name": "gemini",
        "base_url": "https://example.test/v1",
        "model": "bad-model",
        "models": ["bad-model", "good-model"],
        "keys": ["sk-test"],
        "protocol": "openai",
    }
    monkeypatch.setattr(router, "PROVIDERS", [provider])
    router.pool.pools = {
        "gemini": {
            "bad-model": __import__("collections").deque(
                [{"key": "sk-test", "cool_until": 0.0}]),
            "good-model": __import__("collections").deque(
                [{"key": "sk-test", "cool_until": 0.0}]),
        }
    }

    forwarded = []

    class _Ok:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }

    def fake_forward(p, key, payload, streaming, model):
        forwarded.append(model)
        return _Ok()

    monkeypatch.setattr(router, "forward", fake_forward)
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: [
        {"provider": provider, "model": "bad-model"},
        {"provider": provider, "model": "good-model"},
    ])
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(
        router.rate_limiter, "check_and_consume", lambda *a, **k: (True, 0.0))
    monkeypatch.setattr(router.rate_limiter, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "release_reservation", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "set", lambda *a, **k: None)

    result = router._route_completion(
        {"messages": [{"role": "user", "content": "hi"}], "model": "hermes-router"},
        streaming=False,
        ns="",
    )
    assert result[0] == "json"
    assert "bad-model" not in forwarded
    assert "good-model" in forwarded
