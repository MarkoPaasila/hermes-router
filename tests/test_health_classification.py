"""402 / model-gated 403 must not trip health or cool keys; real auth 403 still does."""
from unittest.mock import MagicMock
import router


def _two_candidates():
    a = {"name": "prov_a", "base_url": "https://a.test/v1", "model": "m1",
         "models": ["m1"], "keys": ["sk-a"]}
    b = {"name": "prov_b", "base_url": "https://b.test/v1", "model": "m2",
         "models": ["m2"], "keys": ["sk-b"]}
    return (
        [{"provider": a, "model": "m1"}, {"provider": b, "model": "m2"}],
        a, b,
    )


def _stub_route(monkeypatch, *, track_health=True):
    health = {"fails": [], "ok": []}
    cooled = []

    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router, "_estimated_tokens", lambda m: 10)
    monkeypatch.setattr(router, "_effective_input_cap_for", lambda *a, **k: None)
    monkeypatch.setattr(router, "_hard_input_cap_for", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router, "_completion_has_output", lambda d: True)
    monkeypatch.setattr(router, "_strip_response", lambda d: None)
    monkeypatch.setattr(router, "_add_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router, "_learn_token_cap_from_success", lambda **k: None)
    monkeypatch.setattr(router, "_learn_token_cap_from_error", lambda **k: None)
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "check_and_consume",
                        lambda *a, **k: (True, 0.0))
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router.rate_limiter, "release_reservation", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_429", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "restore", lambda *a, **k: None)

    if track_health:
        def _rh(name, ok):
            (health["ok"] if ok else health["fails"]).append(name)

        monkeypatch.setattr(router.stats, "record_health", _rh)
    else:
        monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"

        def ready_in(self, name, model):
            return 0.0

        def mark_key_down(self, *a, **k):
            cooled.append((a, k))

        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())
    return health, cooled


def _ok_body():
    return {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_http_402_skips_provider_without_health(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    health, cooled = _stub_route(monkeypatch)
    calls = {"n": 0}

    def fake_forward(provider, key, payload, streaming, model, **kwargs):
        calls["n"] += 1
        resp = MagicMock()
        resp.headers = {}
        if provider["name"] == "prov_a":
            resp.status_code = 402
            resp.text = "Payment Required"
            return resp
        resp.status_code = 200
        resp.json.return_value = _ok_body()
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-402")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s.get("reason") == "http_402" and s["provider"] == "prov_a"
               for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"
    assert "prov_a" not in health["fails"]
    assert cooled == []


def test_http_403_agentic_harness_skips_model_without_health(monkeypatch):
    # Same provider, two models — first is harness-gated, second succeeds.
    a = {"name": "openrouter", "base_url": "https://or.test/v1",
         "model": "gated", "models": ["gated", "ok"], "keys": ["sk-or"]}
    ordered = [{"provider": a, "model": "gated"}, {"provider": a, "model": "ok"}]
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    health, cooled = _stub_route(monkeypatch)

    def fake_forward(provider, key, payload, streaming, model, **kwargs):
        resp = MagicMock()
        resp.headers = {}
        if model == "gated":
            resp.status_code = 403
            resp.text = (
                '{"error":{"message":"thinkingmachines/inkling-small:free is only '
                'available on agentic harnesses.","code":403}}'
            )
            return resp
        resp.status_code = 200
        resp.json.return_value = _ok_body()
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-403-model")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s["provider"] == "openrouter" and s["model"] == "gated"
               and s["reason"] == "http_403" for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"
    assert "openrouter" not in health["fails"]
    assert cooled == []


def test_http_403_invalid_key_records_health(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    health, cooled = _stub_route(monkeypatch)

    def fake_forward(provider, key, payload, streaming, model, **kwargs):
        resp = MagicMock()
        resp.headers = {}
        if provider["name"] == "prov_a":
            resp.status_code = 403
            resp.text = '{"error":{"message":"Invalid API key"}}'
            return resp
        resp.status_code = 200
        resp.json.return_value = _ok_body()
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-403-auth")
    assert result[0] == "json"
    assert "prov_a" in health["fails"]
    assert cooled == []  # auth path skips provider, does not mark_key_down
