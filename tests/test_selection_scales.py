"""Complexity / capability scales (CONTEXT.md / ADR-0001): higher = more."""
import json

import router


def test_classify_complexity_trivial_is_one():
    msgs = [{"role": "user", "content": "what year was the moon landing"}]
    assert router.classify_complexity(msgs) == 1


def test_classify_complexity_hard_is_five():
    msgs = [{"role": "user", "content": "implement and debug a refactor of this algorithm:\n```\ndef f():\n  pass\n```"}]
    assert router.classify_complexity(msgs) == 5


def test_rate_model_strong_is_five():
    assert router._rate_model("gemini-2.5-pro") == 5
    assert router._rate_model("claude-opus-4") == 5


def test_rate_model_weak_pattern_is_one():
    assert router._rate_model("some-micro-1b-chat") == 1


def test_smart_ordered_easy_prefers_weaker_capable(monkeypatch):
    weak = {"name": "weak", "model": "lite", "models": ["lite"], "keys": ["k1"]}
    strong = {"name": "strong", "model": "pro", "models": ["pro"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([weak, strong]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_price_rank", lambda m: 0)
    monkeypatch.setattr(router, "_quality_rank", lambda n, m: 0)
    caps = {
        ("weak", "lite"): {"rating": 2, "supports_tools": True, "reasoning": False},
        ("strong", "pro"): {"rating": 5, "supports_tools": True, "reasoning": False},
    }
    monkeypatch.setattr(router, "_model_caps", lambda n, m: caps[(n, m)])
    monkeypatch.setattr(router, "_provider_state", {
        "weak": {"available": True}, "strong": {"available": True},
    })
    ordered = router._get_smart_ordered([weak, strong], complexity=1)
    assert ordered[0]["provider"]["name"] == "weak"


def test_smart_ordered_hard_prefers_stronger(monkeypatch):
    weak = {"name": "weak", "model": "lite", "models": ["lite"], "keys": ["k1"]}
    strong = {"name": "strong", "model": "pro", "models": ["pro"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([weak, strong]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_price_rank", lambda m: 0)
    monkeypatch.setattr(router, "_quality_rank", lambda n, m: 0)
    caps = {
        ("weak", "lite"): {"rating": 2, "supports_tools": True, "reasoning": False},
        ("strong", "pro"): {"rating": 5, "supports_tools": True, "reasoning": False},
    }
    monkeypatch.setattr(router, "_model_caps", lambda n, m: caps[(n, m)])
    monkeypatch.setattr(router, "_provider_state", {
        "weak": {"available": True}, "strong": {"available": True},
    })
    ordered = router._get_smart_ordered([weak, strong], complexity=5)
    assert ordered[0]["provider"]["name"] == "strong"


def test_fast_preference_local_on_easy(monkeypatch):
    local = {"name": "local", "model": "llama", "models": ["llama"], "keys": []}
    cloud = {"name": "groq", "model": "llama", "models": ["llama"], "keys": ["k1"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([local, cloud]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps",
                        lambda n, m: {"rating": 3, "supports_tools": True, "reasoning": False})
    monkeypatch.setattr(router, "_provider_state", {
        "local": {"available": True}, "groq": {"available": True},
    })
    ordered = router._get_smart_ordered(
        [cloud, local], complexity=2, prefer_local=True)
    assert ordered[0]["provider"]["name"] == "local"


def test_migrate_capability_scale_flips_ratings():
    doc = {
        "providers": {"gemini": {"rating": 1, "model": "gemini-2.5-pro"}},
        "model_state": {
            "gemini::gemini-2.5-pro": {"rating": 1, "supports_tools": True},
            "gemini::flash-lite": {"rating": 3, "supports_tools": True},
        },
    }
    out = router._migrate_capability_scale(doc)
    assert out["scale_version"] == router.CAPABILITY_SCALE_VERSION
    assert out["providers"]["gemini"]["rating"] == 5
    assert out["model_state"]["gemini::gemini-2.5-pro"]["rating"] == 5
    assert out["model_state"]["gemini::flash-lite"]["rating"] == 3
    # Idempotent
    again = router._migrate_capability_scale(out)
    assert again["providers"]["gemini"]["rating"] == 5
