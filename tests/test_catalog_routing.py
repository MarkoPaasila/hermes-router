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
