"""Exhausted cascade retry: keys_cooling wait + circuit-open probe."""
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


def _ok_body():
    return {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


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


def test_keys_cooling_short_wait_retries_and_succeeds(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(router, "RATE_EXHAUSTED_WAIT_S", 60.0)

    sleeps = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleeps.append(s))

    ready = {"m1": 5.0, "m2": 8.0}
    pass_n = {"n": 0}

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model, preferred=None):
            # First cascade pass: all cooling; after sleep (second pass): ready.
            if pass_n["n"] == 0:
                return None
            return f"sk-{name}"

        def ready_in(self, name, model):
            return ready.get(model, 0.0)

        def mark_key_down(self, *a, **k):
            pass

        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())

    def fake_forward(provider, key, payload, streaming, model, **kwargs):
        pass_n["n"] += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = _ok_body()
        resp.text = ""
        return resp

    # Detect second _route_completion invocation by wrapping once the sleep happened.
    orig = router._route_completion
    calls = {"n": 0}

    def wrapped(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            pass_n["n"] = 1  # keys become available on exhausted retry
        return orig(*a, **k)

    monkeypatch.setattr(router, "_route_completion", wrapped)
    monkeypatch.setattr(router, "forward", fake_forward)

    result = wrapped(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="keys-cool",
    )
    assert result[0] == "json"
    assert sleeps == [5.0]
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s.get("reason") == "keys_cooling" for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"


def test_circuit_open_probe_on_exhausted_retry_no_sleep(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)

    sleeps = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleeps.append(s))

    # prov_a healthy but hard-fails; prov_b circuit open on first pass.
    monkeypatch.setattr(
        router.stats, "breaker_open",
        lambda n: n == "prov_b",
    )

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"

        def ready_in(self, name, model):
            return 0.0

        def mark_key_down(self, *a, **k):
            pass

        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())

    def fake_forward(provider, key, payload, streaming, model, **kwargs):
        resp = MagicMock()
        resp.headers = {}
        resp.text = "boom"
        if provider["name"] == "prov_a":
            resp.status_code = 500
            return resp
        resp.status_code = 200
        resp.json.return_value = _ok_body()
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)

    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="breaker-probe",
    )
    assert result[0] == "json"
    assert sleeps == []
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s.get("reason") == "circuit_open" for s in fields["cascade"])
    assert fields["cascade"][-1] == {
        "provider": "prov_b", "model": "m2", "outcome": "success", "reason": None,
    }


def test_keys_cooling_above_cap_no_circuit_no_retry(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(router, "RATE_EXHAUSTED_WAIT_S", 60.0)

    sleeps = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleeps.append(s))

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model, preferred=None):
            return None

        def ready_in(self, name, model):
            return 120.0

        def mark_key_down(self, *a, **k):
            pass

        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())
    monkeypatch.setattr(
        router, "forward",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not forward")),
    )

    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="keys-cap",
    )
    assert result[0] == "error"
    assert result[2] == 503
    assert sleeps == []


def test_rate_hold_exhausted_retry_still_works(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(router, "RATE_EXHAUSTED_WAIT_S", 60.0)
    monkeypatch.setattr(router, "RATE_ADMIT_WAIT_S", 0.0)

    sleeps = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleeps.append(s))

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"

        def ready_in(self, name, model):
            return 0.0

        def mark_key_down(self, *a, **k):
            pass

        def peek_key(self, name, model):
            return f"sk-{name}"

    monkeypatch.setattr(router, "pool", _Pool())

    admit_pass = {"n": 0}

    def fake_check(name, key, model, req_count=1.0, token_count=1.0, force=False):
        # First cascade: both on rate hold. After exhausted sleep: admit.
        if admit_pass["n"] == 0:
            return False, 12.0
        return True, 0.0

    monkeypatch.setattr(router.rate_limiter, "check_and_consume", fake_check)

    def fake_forward(provider, key, payload, streaming, model, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = _ok_body()
        resp.text = ""
        return resp

    orig = router._route_completion
    calls = {"n": 0}

    def wrapped(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            admit_pass["n"] = 1
        return orig(*a, **k)

    monkeypatch.setattr(router, "_route_completion", wrapped)
    monkeypatch.setattr(router, "forward", fake_forward)

    result = wrapped(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="rate-retry",
    )
    assert result[0] == "json"
    assert 12.0 in sleeps
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s.get("reason") == "rate_hold" for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"


def test_embeddings_breaker_probe_on_exhausted_retry(monkeypatch):
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
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: n == "emb_b")
    monkeypatch.setattr(router.stats, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_error", lambda *a, **k: None)
    monkeypatch.setattr(router, "_add_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router.key_usage, "add_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "check_and_consume",
                        lambda *a, **k: (True, 0.0))
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router.rate_limiter, "release_reservation", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_429", lambda *a, **k: None)

    sleeps = []
    monkeypatch.setattr(router.time, "sleep", lambda s: sleeps.append(s))

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model, preferred=None):
            return f"sk-{name}"

        def ready_in(self, name, model):
            return 0.0

        def mark_key_down(self, *a, **k):
            pass

    monkeypatch.setattr(router, "pool", _Pool())

    def fake_fwd(provider, key, payload):
        resp = MagicMock()
        resp.headers = {}
        resp.text = "err"
        if provider["name"] == "emb_a":
            resp.status_code = 500
            return resp
        resp.status_code = 200
        resp.json.return_value = {
            "data": [{"embedding": [0.1]}],
            "usage": {"total_tokens": 3},
        }
        return resp

    monkeypatch.setattr(router, "forward_embeddings", fake_fwd)
    client = router.app.test_client()
    r = client.post("/v1/embeddings",
                    json={"input": "hello", "model": "text-embedding-004"},
                    headers={"Authorization": "Bearer sk-test-xxxxxx"})
    assert r.status_code == 200
    assert sleeps == []
    e = router.request_log.snapshot(limit=5)[-1]
    assert e["status"] == "success"
    assert any(s["reason"] == "circuit_open" for s in e["cascade"])
    assert e["cascade"][-1]["outcome"] == "success"
    assert e["provider"] == "emb_b"
