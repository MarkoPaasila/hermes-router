from unittest.mock import MagicMock
import router
from cascade_trail import CascadeTrail


def _two_candidates():
    a = {"name": "prov_a", "base_url": "https://a.test/v1", "model": "m1",
         "models": ["m1"], "keys": ["sk-a"]}
    b = {"name": "prov_b", "base_url": "https://b.test/v1", "model": "m2",
         "models": ["m2"], "keys": ["sk-b"]}
    return (
        [{"provider": a, "model": "m1"}, {"provider": b, "model": "m2"}],
        a, b,
    )


def _stub_common(monkeypatch):
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
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
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

    class _Pool:
        def key_count(self, name, model):
            return 1
        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"
        def mark_key_down(self, *a, **k):
            pass
        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())


def test_token_cap_skip_then_success(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        router, "_hard_input_cap_for",
        lambda provider, model: 5 if provider["name"] == "prov_a" else None,
    )
    monkeypatch.setattr(router, "_estimated_tokens", lambda m: 100)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        assert provider["name"] == "prov_b"
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-cascade-cap")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert fields["cascade"][0]["outcome"] == "skipped"
    assert fields["cascade"][0]["reason"] == "token_cap"
    assert fields["cascade"][0]["provider"] == "prov_a"
    assert fields["cascade"][-1]["outcome"] == "success"
    assert fields["skipped"] == 1
    assert fields["failed"] == 0
    assert fields["cascades"] == 1


def test_http_429_then_success(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    calls = {"n": 0}
    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        calls["n"] += 1
        resp = MagicMock()
        resp.headers = {}
        if calls["n"] == 1:
            resp.status_code = 429
            resp.text = "rate"
            return resp
        resp.status_code = 200
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-cascade-429")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s["outcome"] == "failed" and s["reason"] == "http_429"
               for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"
    assert fields["failed"] >= 1
    assert fields["cascades"] == fields["failed"] + fields["skipped"]


def test_rate_hold_skip_classified_as_skipped(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)

    def fake_check(name, key, model, req_count=1.0, token_count=1.0, force=False):
        # Simulate Retry-After hold: deny even when force=True.
        if name == "prov_a":
            return False, 60.0
        return True, 0.0

    monkeypatch.setattr(router.rate_limiter, "check_and_consume", fake_check)
    monkeypatch.setattr(router.rate_limiter, "headroom",
                        lambda name, key, model: 0.0 if name == "prov_a" else 1.0)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        assert provider["name"] == "prov_b"
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="test-cascade-rl")
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s["outcome"] == "skipped" and s["reason"] == "rate_hold"
               for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"


def test_log_completion_includes_cascade_fields():
    trail = CascadeTrail()
    trail.skip("groq", "m", "rate_headroom")
    trail.success("mistral", "m2")
    router._req_ctx.cascade = trail
    router._req_ctx.cache_hit = False
    router._req_ctx.attempts = 1
    router._req_ctx.provider = "mistral"
    router._req_ctx.model = "m2"
    router._req_ctx.last_tried_provider = "mistral"
    router._req_ctx.last_tried_model = "m2"
    entry = router._log_completion(
        "sk-test-xxxxxx", "chat",
        {"messages": [{"role": "user", "content": "x"}], "stream": False},
        ("json", {"usage": {}}), 0.01)
    assert entry is not None
    assert entry["failed"] == 0
    assert entry["skipped"] == 1
    assert entry["cascades"] == 1
    assert len(entry["cascade"]) == 2


def test_embeddings_cache_hit_has_empty_cascade_fields(monkeypatch):
    router.request_log.clear()
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: {"data": [], "object": "list"})
    monkeypatch.setattr(router, "_auth_check", lambda: None)
    monkeypatch.setattr(router, "_admit_request", lambda token: None)
    monkeypatch.setattr(router, "_caller_token", lambda: "sk-test-xxxxxx")
    monkeypatch.setattr(router, "_embed_ordered", lambda: [
        {"name": "gemini", "embed_model": "text-embedding-004", "keys": ["k"]},
    ])
    client = router.app.test_client()
    r = client.post("/v1/embeddings",
                    json={"input": "hello", "model": "text-embedding-004"},
                    headers={"Authorization": "Bearer sk-test-xxxxxx"})
    assert r.status_code == 200
    entries = router.request_log.snapshot(limit=5)
    assert entries
    e = entries[-1]
    assert e["status"] == "cache_hit"
    assert e["cascade"] == []
    assert e["failed"] == 0
    assert e["skipped"] == 0
    assert e["cascades"] == 0


def test_embeddings_success_after_headroom_skip_counts_skipped(monkeypatch):
    router.request_log.clear()
    p_a = {"name": "emb_a", "embed_model": "e1", "keys": ["ka"],
           "base_url": "https://a.test/v1"}
    p_b = {"name": "emb_b", "embed_model": "e2", "keys": ["kb"],
           "base_url": "https://b.test/v1"}
    monkeypatch.setattr(router, "_auth_check", lambda: None)
    monkeypatch.setattr(router, "_admit_request", lambda token: None)
    monkeypatch.setattr(router, "_caller_token", lambda: "sk-test-xxxxxx")
    monkeypatch.setattr(router, "_embed_ordered", lambda: [p_a, p_b])
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(router, "_add_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router.key_usage, "add_tokens", lambda *a, **k: None)

    def fake_check(name, key, model, req_count=1.0, token_count=1.0, force=False):
        if name == "emb_a":
            return False, 60.0
        return True, 0.0

    monkeypatch.setattr(router.rate_limiter, "check_and_consume", fake_check)
    monkeypatch.setattr(router.rate_limiter, "headroom",
                        lambda name, key, model: 0.0 if name == "emb_a" else 1.0)
    monkeypatch.setattr(router.rate_limiter, "release_reservation", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)

    class _Pool:
        def key_count(self, name, model):
            return 1
        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"
        def mark_key_down(self, *a, **k):
            pass

    monkeypatch.setattr(router, "pool", _Pool())

    def fake_fwd(provider, key, payload):
        assert provider["name"] == "emb_b"
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {
            "data": [{"embedding": [0.1]}],
            "usage": {"total_tokens": 3},
        }
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward_embeddings", fake_fwd)
    client = router.app.test_client()
    r = client.post("/v1/embeddings",
                    json={"input": "hello", "model": "text-embedding-004"},
                    headers={"Authorization": "Bearer sk-test-xxxxxx"})
    assert r.status_code == 200
    e = router.request_log.snapshot(limit=5)[-1]
    assert e["status"] == "success"
    assert e["skipped"] >= 1
    assert any(s["reason"] == "rate_hold" for s in e["cascade"])
    assert e["cascade"][-1]["outcome"] == "success"
    assert e["cascades"] == e["failed"] + e["skipped"]
