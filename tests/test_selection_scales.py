"""Complexity / GI scales (CONTEXT.md / ADR-0002): higher GI = stronger."""
import json

import gi_ranking
import router


def _seed_classic_min_gi(monkeypatch):
    """Fixed 0/20/40/60/80 bars; skip catalog refresh so monkeypatched caps stay in play."""
    gi_ranking.recompute_complexity_thresholds([0.0, 20.0, 40.0, 60.0, 80.0, 100.0])
    monkeypatch.setattr(router, "_refresh_complexity_thresholds_if_needed", lambda providers=None: None)


def test_classify_complexity_trivial_is_one():
    msgs = [{"role": "user", "content": "what year was the moon landing"}]
    assert router.classify_complexity(msgs) == 1


def test_classify_complexity_hard_is_five():
    msgs = [{"role": "user", "content": "implement and debug a refactor of this algorithm:\n```\ndef f():\n  pass\n```"}]
    assert router.classify_complexity(msgs) == 5


def test_smart_ordered_easy_prefers_weaker_eligible(monkeypatch):
    _seed_classic_min_gi(monkeypatch)
    weak = {"name": "weak", "model": "lite", "models": ["lite"], "keys": ["k1"]}
    strong = {"name": "strong", "model": "pro", "models": ["pro"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([weak, strong]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_price_rank", lambda m: 0)
    caps = {
        ("weak", "lite"): {"gi": 25.0, "gi_source": "snapshot", "supports_tools": True, "reasoning": False},
        ("strong", "pro"): {"gi": 90.0, "gi_source": "snapshot", "supports_tools": True, "reasoning": False},
    }
    monkeypatch.setattr(router, "_model_caps", lambda n, m: caps[(n, m)])
    monkeypatch.setattr(router, "_provider_state", {
        "weak": {"available": True}, "strong": {"available": True},
    })
    # complexity 1 → min GI 0; both eligible; least overshoot prefers weak (25 vs 90)
    ordered = router._get_smart_ordered([weak, strong], complexity=1)
    assert ordered[0]["provider"]["name"] == "weak"


def test_smart_ordered_hard_prefers_stronger(monkeypatch):
    _seed_classic_min_gi(monkeypatch)
    weak = {"name": "weak", "model": "lite", "models": ["lite"], "keys": ["k1"]}
    strong = {"name": "strong", "model": "pro", "models": ["pro"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([weak, strong]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_price_rank", lambda m: 0)
    caps = {
        ("weak", "lite"): {"gi": 25.0, "gi_source": "snapshot", "supports_tools": True, "reasoning": False},
        ("strong", "pro"): {"gi": 90.0, "gi_source": "snapshot", "supports_tools": True, "reasoning": False},
    }
    monkeypatch.setattr(router, "_model_caps", lambda n, m: caps[(n, m)])
    monkeypatch.setattr(router, "_provider_state", {
        "weak": {"available": True}, "strong": {"available": True},
    })
    # complexity 5 → min GI 80; only strong is eligible (tier 0)
    ordered = router._get_smart_ordered([weak, strong], complexity=5)
    assert ordered[0]["provider"]["name"] == "strong"


def test_fast_preference_local_on_easy(monkeypatch):
    _seed_classic_min_gi(monkeypatch)
    local = {"name": "local", "model": "llama", "models": ["llama"], "keys": []}
    cloud = {"name": "groq", "model": "llama", "models": ["llama"], "keys": ["k1"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([local, cloud]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_model_caps",
                        lambda n, m: {"gi": 50.0, "gi_source": "snapshot",
                                      "supports_tools": True, "reasoning": False})
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
    again = router._migrate_capability_scale(out)
    assert again["providers"]["gemini"]["rating"] == 5


def test_cheapest_among_eligible(monkeypatch):
    _seed_classic_min_gi(monkeypatch)
    cheap = {"name": "cheap", "model": "ok", "models": ["ok"], "keys": ["k1"]}
    pricey = {"name": "pricey", "model": "ok2", "models": ["ok2"], "keys": ["k2"]}
    monkeypatch.setattr(router, "pool", router.CredentialPool([cheap, pricey]))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_price_rank", lambda m: 1.0 if m == "ok2" else 0.0)
    caps = {
        ("cheap", "ok"): {"gi": 85.0, "gi_source": "snapshot", "supports_tools": True, "reasoning": False},
        ("pricey", "ok2"): {"gi": 90.0, "gi_source": "snapshot", "supports_tools": True, "reasoning": False},
    }
    monkeypatch.setattr(router, "_model_caps", lambda n, m: caps[(n, m)])
    monkeypatch.setattr(router, "_provider_state", {
        "cheap": {"available": True}, "pricey": {"available": True},
    })
    ordered = router._get_smart_ordered([pricey, cheap], complexity=5)
    assert ordered[0]["provider"]["name"] == "cheap"


def test_min_gi_thresholds():
    gi_ranking.recompute_complexity_thresholds([0.0, 20.0, 40.0, 60.0, 80.0, 100.0])
    assert gi_ranking.min_gi_for_complexity(1) == 0.0
    assert gi_ranking.min_gi_for_complexity(5) == 80.0


def test_smart_ordered_refreshes_thresholds_from_catalog(monkeypatch, tmp_path):
    """Dirty flag + resolve_gi catalog → complexity 5 uses 80th percentile of that set."""
    snap = tmp_path / "gi_rankings.json"
    snap.write_text(json.dumps({
        "version": 1,
        "models": {
            "a": {"gi": 10.0},
            "b": {"gi": 30.0},
            "c": {"gi": 50.0},
            "d": {"gi": 70.0},
            "e": {"gi": 90.0},
        },
    }))
    monkeypatch.setenv("GI_RANKINGS_FILE", str(snap))
    monkeypatch.setenv("GI_OVERRIDES_FILE", str(tmp_path / "gi_overrides.json"))
    gi_ranking.reset_for_tests()
    gi_ranking.load_snapshot(force=True)
    gi_ranking.mark_complexity_thresholds_dirty()

    providers = [
        {"name": "p", "model": "a", "models": ["a", "b", "c", "d", "e"], "keys": ["k"]},
    ]
    monkeypatch.setattr(router, "pool", router.CredentialPool(providers))
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)
    monkeypatch.setattr(router.stats, "health_bucket", lambda n: 0)
    monkeypatch.setattr(router.rate_limiter, "headroom", lambda *a, **k: 1.0)
    monkeypatch.setattr(router, "_price_rank", lambda m: 0)
    monkeypatch.setattr(router, "_provider_state", {"p": {"available": True}})

    def _caps(n, m):
        gi, src = gi_ranking.resolve_gi(n, m)
        return {"gi": gi, "gi_source": src, "supports_tools": True, "reasoning": False}

    monkeypatch.setattr(router, "_model_caps", _caps)

    router._get_smart_ordered(providers, complexity=5)
    # n=5, p=80 → index ceil(4)-1 = 3 → 70
    assert gi_ranking.min_gi_for_complexity(5) == 70.0
    assert gi_ranking.complexity_thresholds_need_refresh() is False
