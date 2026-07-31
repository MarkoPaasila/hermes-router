import router


def _pool_two_keys():
    providers = [{
        "name": "groq",
        "model": "llama",
        "models": ["llama"],
        "keys": ["key-aaaaaaa1", "key-bbbbbbb2"],
    }]
    return router.CredentialPool(providers)


def test_peek_key_does_not_advance_or_count():
    pool = _pool_two_keys()
    a = pool.peek_key("groq", "llama")
    b = pool.peek_key("groq", "llama")
    assert a == b == "key-aaaaaaa1"
    assert pool.key_requests_for("groq", "key-aaaaaaa1") == 0


def test_get_key_prefers_sticky_when_ready():
    pool = _pool_two_keys()
    k = pool.get_key("groq", "llama", preferred="key-bbbbbbb2")
    assert k == "key-bbbbbbb2"
    assert pool.key_requests_for("groq", "key-bbbbbbb2") == 1


def test_get_key_falls_back_when_preferred_cooling():
    pool = _pool_two_keys()
    pool.mark_key_down("groq", "key-bbbbbbb2", retry_after=60)
    k = pool.get_key("groq", "llama", preferred="key-bbbbbbb2")
    assert k == "key-aaaaaaa1"


def test_smart_ordered_no_provider_rotation(monkeypatch):
    # Two equal-tier providers; without RR offset, order must be stable across calls
    p1 = {"name": "a", "model": "m1", "models": ["m1"], "keys": ["k1"]}
    p2 = {"name": "b", "model": "m1", "models": ["m1"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([p1, p2]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps", lambda n, m: {"rating": 3, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {"a": {"available": True}, "b": {"available": True}})
    o1 = [c["provider"]["name"] for c in router._get_smart_ordered([p1, p2], complexity=1)]
    o2 = [c["provider"]["name"] for c in router._get_smart_ordered([p1, p2], complexity=1)]
    assert o1 == o2


def test_smart_ordered_sticky_first(monkeypatch):
    p1 = {"name": "a", "model": "m1", "models": ["m1"], "keys": ["k1"]}
    p2 = {"name": "b", "model": "m2", "models": ["m2"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([p1, p2]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps", lambda n, m: {"rating": 3, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {"a": {"available": True}, "b": {"available": True}})
    sticky = {"provider": "b", "model": "m2", "key": "k2"}
    ordered = router._get_smart_ordered([p1, p2], complexity=1, sticky=sticky)
    assert ordered[0]["provider"]["name"] == "b"
    assert ordered[0]["model"] == "m2"


def test_remember_and_clear_sticky():
    router.sticky_store = router.SessionStickyStore(ttl_s=3600, max_entries=100)
    router._remember_sticky("sess", "groq", "llama", "key1")
    assert router.sticky_store.get("sess")["key"] == "key1"
    router._clear_sticky("sess")
    assert router.sticky_store.get("sess") is None


def test_sticky_for_request_reads_header():
    sid, st = router._sticky_for_request(
        {"X-Hermes-Session-Id": "abc"}, {"messages": []})
    assert sid == "abc"
    assert st is None


def test_sticky_for_request_loads_store(monkeypatch):
    store = router.SessionStickyStore(ttl_s=3600, max_entries=100)
    store.set("abc", provider="groq", model="llama", key="k1")
    monkeypatch.setattr(router, "sticky_store", store)
    sid, st = router._sticky_for_request(
        {"X-Hermes-Session-Id": "abc"}, {"messages": []})
    assert sid == "abc"
    assert st["provider"] == "groq"
    assert st["model"] == "llama"
    assert st["key"] == "k1"


def test_provider_model_default_removed():
    assert hasattr(router, "PROVIDER_MODEL_DEFAULT") is False


def test_dashboard_html_has_tbf_on_providers_and_models_not_standalone():
    with open("router.py", encoding="utf-8") as f:
        src = f.read()
    assert 'id="page-rate-limits"' not in src
    assert 'data-page="rate-limits"' not in src
    pages_line = src.split("const PAGES")[1].split(";")[0]
    assert "rate-limits" not in pages_line
    assert 'id="page-providers"' in src and 'id="page-models"' in src
    assert 'id="rl-tbody-pw"' in src
    assert 'id="rl-tbody-model"' in src


def test_ordered_providers_passes_sticky(monkeypatch):
    captured = {}

    def _capture(providers, complexity, est_tokens=0, prefer_local=False, sticky=None):
        captured["sticky"] = sticky
        return []

    monkeypatch.setattr(router, "_get_smart_ordered", _capture)
    sticky = {"provider": "groq", "model": "llama", "key": "k1"}
    router._ordered_providers({"messages": []}, sticky=sticky)
    assert captured["sticky"] == sticky


def test_anthropic_messages_passes_sticky_kwargs(monkeypatch):
    store = router.SessionStickyStore(ttl_s=3600, max_entries=100)
    store.set("sess-msg", provider="groq", model="llama", key="k1")
    monkeypatch.setattr(router, "sticky_store", store)
    captured = {}

    def fake_route(payload, streaming, cache_ns, _session_id=None, _sticky=None):
        captured["session_id"] = _session_id
        captured["sticky"] = _sticky
        return ("json", {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    monkeypatch.setattr(router, "_route_completion", fake_route)
    monkeypatch.setattr(router, "_auth_check", lambda: None)
    monkeypatch.setattr(router, "_admit_request", lambda token: None)
    monkeypatch.setattr(router, "_record_request_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router, "_log_completion", lambda *a, **k: None)

    client = router.app.test_client()
    resp = client.post(
        "/v1/messages",
        json={"messages": [{"role": "user", "content": "hello"}]},
        headers={"X-Hermes-Session-Id": "sess-msg"},
    )
    assert resp.status_code == 200
    assert captured["session_id"] == "sess-msg"
    assert captured["sticky"]["provider"] == "groq"
    assert captured["sticky"]["model"] == "llama"
    assert captured["sticky"]["key"] == "k1"
