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
    monkeypatch.setattr(router, "_model_caps", lambda n, m: {"rating": 5, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {"a": {"available": True}, "b": {"available": True}})
    o1 = [c["provider"]["name"] for c in router._get_smart_ordered([p1, p2], complexity=5)]
    o2 = [c["provider"]["name"] for c in router._get_smart_ordered([p1, p2], complexity=5)]
    assert o1 == o2


def test_smart_ordered_sticky_first(monkeypatch):
    p1 = {"name": "a", "model": "m1", "models": ["m1"], "keys": ["k1"]}
    p2 = {"name": "b", "model": "m2", "models": ["m2"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([p1, p2]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps", lambda n, m: {"rating": 5, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {"a": {"available": True}, "b": {"available": True}})
    sticky = {"provider": "b", "model": "m2", "key": "k2"}
    ordered = router._get_smart_ordered([p1, p2], complexity=5, sticky=sticky)
    assert ordered[0]["provider"]["name"] == "b"
    assert ordered[0]["model"] == "m2"
