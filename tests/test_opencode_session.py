from unittest.mock import MagicMock

import pytest

import opencode_session as oc
import router
from ttft_baseline import TtftBaselineStore


def _opencode_provider(**overrides):
    p = {
        "name": "opencode",
        "base_url": "https://opencode.ai/zen/v1",
        "model": "deepseek-v4-flash-free",
        "headers": {},
    }
    p.update(overrides)
    return p


def _groq_provider():
    return {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "m",
        "headers": {},
    }


@pytest.fixture(autouse=True)
def _reset_opencode_session_ctx():
    oc.reset_request_session()
    yield
    oc.reset_request_session()


@pytest.fixture
def _fresh_forward(monkeypatch):
    store = TtftBaselineStore(
        floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=1.0,
    )
    monkeypatch.setattr(router, "ttft_baselines", store)
    monkeypatch.setattr(router, "_model_caps", lambda *a, **k: {})
    monkeypatch.setattr(router, "_apply_output_token_cap", lambda *a, **k: None)
    monkeypatch.setattr(router, "_extend_response_read_timeout", lambda *a, **k: None)
    return store


def test_is_opencode_target_by_provider_name():
    assert oc.is_opencode_target("opencode", None) is True
    assert oc.is_opencode_target("opencode_go", None) is True
    assert oc.is_opencode_target("groq", None) is False


def test_is_opencode_target_by_opencode_host():
    assert oc.is_opencode_target(
        "local", "https://opencode.ai/zen/go/v1") is True
    assert oc.is_opencode_target(
        None, "https://opencode.ai/zen/v1") is True
    assert oc.is_opencode_target(
        "local", "https://relay.opencode.ai/v1") is True


def test_is_opencode_target_rejects_lookalike_hosts():
    assert oc.is_opencode_target(
        "groq", "https://api.groq.com/openai/v1") is False
    assert oc.is_opencode_target(
        "local", "https://notopencode.ai/v1") is False
    assert oc.is_opencode_target(
        "local", "https://opencode.example.com/v1") is False


def test_merge_sets_header_from_explicit_session_id():
    headers = {"Authorization": "Bearer k"}
    out = oc.merge_opencode_session_headers(
        headers, _opencode_provider(), session_id="sess-abc")
    assert out is headers
    assert headers["x-opencode-session"] == "sess-abc"


def test_merge_leaves_non_opencode_headers_untouched():
    headers = {"Authorization": "Bearer k"}
    oc.merge_opencode_session_headers(headers, _groq_provider(), session_id="sess-abc")
    assert "x-opencode-session" not in headers


def test_merge_existing_header_wins():
    headers = {"x-opencode-session": "pinned"}
    oc.merge_opencode_session_headers(
        headers, _opencode_provider(), session_id="sess-abc")
    assert headers["x-opencode-session"] == "pinned"


def test_merge_without_session_sends_non_empty_and_reuses_value():
    first = {}
    oc.merge_opencode_session_headers(first, _opencode_provider())
    val = first.get("x-opencode-session")
    assert val and isinstance(val, str)
    second = {}
    oc.merge_opencode_session_headers(second, _opencode_provider())
    assert second["x-opencode-session"] == val


def test_forward_opencode_sends_client_session_id(_fresh_forward, monkeypatch):
    captured = {}

    def fake_post(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    router._req_ctx.session_id = "sess-chat"
    oc.seed_request_session("sess-chat")
    router.forward(_opencode_provider(), "sk-test", {"messages": []}, False, "m")
    assert captured["headers"]["x-opencode-session"] == "sess-chat"


def test_forward_opencode_sends_header_without_client_session(_fresh_forward, monkeypatch):
    captured = {}

    def fake_post(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    router.forward(_opencode_provider(), "sk-test", {"messages": []}, False, "m")
    val = captured["headers"].get("x-opencode-session")
    assert val and isinstance(val, str)


def test_forward_groq_does_not_send_opencode_header(_fresh_forward, monkeypatch):
    captured = {}

    def fake_post(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    router.forward(_groq_provider(), "sk-test", {"messages": []}, False, "m")
    assert "x-opencode-session" not in captured["headers"]


def test_probe_provider_sends_opencode_session(monkeypatch):
    captured = {}

    def fake_post(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    router._probe_provider(_opencode_provider(), "sk-test")
    assert captured["headers"].get("x-opencode-session")


def test_discover_models_sends_opencode_session(monkeypatch):
    captured = {}

    def fake_get(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": [{"id": "m1"}]}
        return resp

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    router._discover_models_with_catalog(_opencode_provider(), "sk-test")
    assert captured["headers"].get("x-opencode-session")


def test_fetch_models_catalog_map_sends_opencode_session(monkeypatch):
    captured = {}

    def fake_get(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": []}
        return resp

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    router._fetch_models_catalog_map(_opencode_provider(), "sk-test")
    assert captured["headers"].get("x-opencode-session")


def test_route_completion_seeds_session_for_opencode_header(monkeypatch):
    monkeypatch.setattr(router.cache, "get", lambda *a, **k: {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    })
    router._route_completion(
        {"messages": [{"role": "user", "content": "x"}]}, False,
        _session_id="sess-from-route",
    )
    headers = {}
    oc.merge_opencode_session_headers(headers, _opencode_provider())
    assert headers["x-opencode-session"] == "sess-from-route"


def test_forward_embeddings_sends_opencode_session(monkeypatch):
    captured = {}

    def fake_post(*a, **k):
        captured["headers"] = k.get("headers") or {}
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    p = _opencode_provider()
    p["embed_model"] = "embed-m"
    router.forward_embeddings(p, "sk-test", {"input": "hi"})
    assert captured["headers"].get("x-opencode-session")
