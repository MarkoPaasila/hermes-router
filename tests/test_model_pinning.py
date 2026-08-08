# tests/test_model_pinning.py
from unittest.mock import MagicMock

import router
import gi_ranking as gi


def test_is_auto_model_id():
    assert router._is_auto_model_id(None) is True
    assert router._is_auto_model_id("") is True
    assert router._is_auto_model_id(router.ROUTER_MODEL) is True
    assert router._is_auto_model_id("auto") is True
    assert router._is_auto_model_id(f"{router.ROUTER_MODEL}:fast") is True
    assert router._is_auto_model_id("gemini-2.5-flash") is False
    assert router._is_auto_model_id("google/gemini-2.5-flash") is False


def test_models_match_normalized_strips_org_and_tags():
    assert router._models_match_normalized(
        "gemini-2.5-flash", "google/gemini-2.5-flash:free")
    assert router._models_match_normalized("GPT-4o", "openai/gpt-4o")
    assert not router._models_match_normalized(
        "gemini-2.5-flash", "gemini-2.5-flash-lite")
    assert not router._models_match_normalized("", "gemini-2.5-flash")


def test_filter_candidates_by_pin_preserves_order():
    a = {"name": "openrouter", "model": "google/gemini-2.5-flash"}
    b = {"name": "gemini", "model": "gemini-2.5-pro"}
    c = {"name": "gemini", "model": "gemini-2.5-flash"}
    ordered = [
        {"provider": a, "model": a["model"]},
        {"provider": b, "model": b["model"]},
        {"provider": c, "model": c["model"]},
    ]
    pinned = router._filter_candidates_by_pin(ordered, "gemini-2.5-flash")
    assert [x["provider"]["name"] for x in pinned] == ["openrouter", "gemini"]
    assert [x["model"] for x in pinned] == [
        "google/gemini-2.5-flash", "gemini-2.5-flash"]


def test_chat_catalog_model_ids_unique_first_seen():
    providers = [
        {"name": "gemini", "model": "gemini-2.5-flash",
         "models": ["gemini-2.5-flash", "gemini-2.5-pro"]},
        {"name": "openrouter", "model": "google/gemini-2.5-flash",
         "models": ["google/gemini-2.5-flash", "gemini-2.5-flash"]},
    ]
    ids = router._chat_catalog_model_ids(providers)
    assert ids == [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "google/gemini-2.5-flash",
    ]


def _two_same_logical_model():
    a = {"name": "openrouter", "base_url": "https://or.test/v1",
         "model": "google/gemini-2.5-flash",
         "models": ["google/gemini-2.5-flash"], "keys": ["sk-or"]}
    b = {"name": "gemini", "base_url": "https://g.test/v1",
         "model": "gemini-2.5-flash",
         "models": ["gemini-2.5-flash"], "keys": ["sk-g"]}
    other = {"name": "groq", "base_url": "https://q.test/v1",
             "model": "llama-3.3", "models": ["llama-3.3"], "keys": ["sk-q"]}
    ordered = [
        {"provider": other, "model": "llama-3.3"},
        {"provider": a, "model": "google/gemini-2.5-flash"},
        {"provider": b, "model": "gemini-2.5-flash"},
    ]
    return ordered, a, b, other


def _stub_route(monkeypatch):
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
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
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


def test_pinned_unknown_model_returns_400(monkeypatch):
    _stub_route(monkeypatch)
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: [])
    result = router._route_completion(
        {"model": "totally-unknown-model",
         "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="pin-unknown",
    )
    assert result[0] == "error"
    assert result[2] == 400
    assert result[1]["error"]["type"] == "invalid_request_error"
    assert "totally-unknown-model" in result[1]["error"]["message"]
    assert "/v1/models" in result[1]["error"]["message"]


def test_pinned_only_attempts_matching_candidates(monkeypatch):
    ordered, a, b, other = _two_same_logical_model()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_route(monkeypatch)
    tried = []

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model,
                     first_byte_deadline_s=None):
        tried.append((provider["name"], model))
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "gemini-2.5-flash",
         "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="pin-match",
    )
    assert result[0] == "json"
    assert tried[0][0] in ("openrouter", "gemini")
    assert all(t[0] != "groq" for t in tried)
    assert router._models_match_normalized(tried[0][1], "gemini-2.5-flash")


def test_auto_still_can_try_unrelated_models(monkeypatch):
    ordered, a, b, other = _two_same_logical_model()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_route(monkeypatch)
    tried = []

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model,
                     first_byte_deadline_s=None):
        tried.append(provider["name"])
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": router.ROUTER_MODEL,
         "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="pin-auto",
    )
    assert result[0] == "json"
    assert tried[0] == "groq"  # first in mocked ordered list


def test_pinned_exhaustion_mentions_model(monkeypatch):
    ordered, a, b, other = _two_same_logical_model()
    # Only matching candidates, all fail
    match_only = [c for c in ordered if c["provider"]["name"] != "groq"]
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: match_only)
    _stub_route(monkeypatch)

    def fake_forward(provider, key, payload, streaming, model,
                     first_byte_deadline_s=None):
        resp = MagicMock()
        resp.status_code = 500
        resp.headers = {}
        resp.text = "boom"
        resp.json.return_value = {"error": "boom"}
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "gemini-2.5-flash",
         "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="pin-exh",
    )
    assert result[0] == "error"
    assert result[2] == 503
    assert "gemini-2.5-flash" in result[1]["error"]["message"]
    assert result[1]["error"]["type"] == "router_error"
