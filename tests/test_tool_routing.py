"""Tests for tool-probe honesty, last-resort failover, and tools self-heal."""
from unittest.mock import MagicMock

import router


class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_probe_tools_truncated_response_is_inconclusive(monkeypatch):
    """finish_reason=length with no tool_calls must not cache as tools=no."""
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(200, {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": ""},
            }],
        })

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    provider = {"base_url": "https://example.test/v1", "headers": {}}
    assert router._probe_tools(provider, "sk-test", "nemotron-3-ultra-free") is None


def test_probe_tools_empty_content_is_inconclusive(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(200, {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": ""},
            }],
        })

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    provider = {"base_url": "https://example.test/v1", "headers": {}}
    assert router._probe_tools(provider, "sk-test", "some-model") is None


def test_probe_tools_text_answer_without_tools_is_false(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(200, {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "Paris is lovely in the spring."},
            }],
        })

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    provider = {"base_url": "https://example.test/v1", "headers": {}}
    assert router._probe_tools(provider, "sk-test", "text-only-model") is False


def test_probe_tools_emits_tool_calls_is_true(monkeypatch):
    def fake_post(url, headers=None, json=None, timeout=None):
        return _FakeResp(200, {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [{"id": "1", "type": "function",
                                   "function": {"name": "get_weather", "arguments": "{}"}}],
                },
            }],
        })

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    provider = {"base_url": "https://example.test/v1", "headers": {}}
    assert router._probe_tools(provider, "sk-test", "tools-model") is True


def test_promote_tools_support_flips_cached_false(tmp_path, monkeypatch):
    state = tmp_path / "router_state.json"
    state.write_text(
        '{"last_updated":"2026-01-01T00:00:00","last_updated_ts":0,'
        '"providers":{"opencode":{"model":"nemotron-3-ultra-free","supports_tools":false}},'
        '"model_state":{"opencode::nemotron-3-ultra-free":'
        '{"rating":1,"supports_tools":false,"reasoning":false}}}')
    monkeypatch.setattr(router, "STATE_FILE", state)
    router._model_state = {
        ("opencode", "nemotron-3-ultra-free"): {
            "rating": 1, "supports_tools": False, "reasoning": False},
    }
    router._provider_state = {
        "opencode": {"model": "nemotron-3-ultra-free", "supports_tools": False},
    }
    router._promote_tools_support("opencode", "nemotron-3-ultra-free")
    assert router._model_supports_tools("opencode", "nemotron-3-ultra-free") is True
    import json
    doc = json.loads(state.read_text())
    assert doc["model_state"]["opencode::nemotron-3-ultra-free"]["supports_tools"] is True


def test_response_has_tool_calls():
    assert router._response_has_tool_calls({
        "choices": [{"message": {"tool_calls": [{"id": "1"}]}}],
    }) is True
    assert router._response_has_tool_calls({
        "choices": [{"message": {"content": "hi"}}],
    }) is False
    assert router._response_has_tool_calls(None) is False


def test_tool_last_resort_tries_deferred_candidate(monkeypatch):
    """When the only remaining candidate is tools=no, last-resort still tries it."""
    tried = []

    capable = {
        "name": "capable",
        "base_url": "https://capable.test/v1",
        "model": "good",
        "models": ["good"],
        "keys": ["sk-capable"],
    }
    deferred = {
        "name": "deferred",
        "base_url": "https://deferred.test/v1",
        "model": "maybe",
        "models": ["maybe"],
        "keys": ["sk-deferred"],
    }

    ordered = [
        {"provider": capable, "model": "good"},
        {"provider": deferred, "model": "maybe"},
    ]

    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    monkeypatch.setattr(router, "_model_supports_tools",
                        lambda name, model: name != "deferred")
    monkeypatch.setattr(router, "_estimated_tokens", lambda m: 10)
    monkeypatch.setattr(router, "_effective_input_cap_for", lambda *a, **k: None)
    monkeypatch.setattr(router, "SEMANTIC_CACHE", False)
    monkeypatch.setattr(router.stats, "breaker_open", lambda n: False)

    # capable always rate-blocked; deferred succeeds
    def fake_headroom(name, key, model):
        return 0.0 if name == "capable" else 1.0

    def fake_check(name, key, model, req_count=1.0, token_count=1.0):
        if name == "capable":
            return False, 60.0
        return True, 0.0

    monkeypatch.setattr(router.rate_limiter, "headroom", fake_headroom)
    monkeypatch.setattr(router.rate_limiter, "check_and_consume", fake_check)
    monkeypatch.setattr(router.rate_limiter, "restore", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "update_from_headers", lambda *a, **k: None)
    monkeypatch.setattr(router.rate_limiter, "on_success", lambda *a, **k: None)

    class _Pool:
        def key_count(self, name, model):
            return 1

        def get_key(self, name, model):
            return f"sk-{name}"

        def mark_key_down(self, *a, **k):
            pass

    monkeypatch.setattr(router, "pool", _Pool())

    success_body = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "x", "arguments": "{}"}}],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def fake_forward(provider, key, payload, streaming, model):
        tried.append(f"{provider['name']}/{model}")
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = success_body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    monkeypatch.setattr(router, "_completion_has_output", lambda d: True)
    monkeypatch.setattr(router, "_strip_response", lambda d: None)
    monkeypatch.setattr(router, "_add_provider_tokens", lambda *a, **k: None)
    monkeypatch.setattr(router, "_learn_token_cap_from_success", lambda **k: None)
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(router.cache, "set", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_success", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)
    monkeypatch.setattr(router.stats, "record_error", lambda *a, **k: None)

    # Force tools enforcement path
    payload = {
        "model": "hermes-router",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "x", "parameters": {}}}],
    }

    # _route_completion is the shared pipeline
    result = router._route_completion(payload, streaming=False, ns="test")

    assert result[0] == "json"
    assert "deferred/maybe" in tried
    assert tried[-1] == "deferred/maybe"
