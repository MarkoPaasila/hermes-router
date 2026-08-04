from unittest.mock import MagicMock

import router
from ttft_baseline import TtftDeadlineExceeded


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


def test_ttft_abort_clears_sticky_and_cascades(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(router, "ttft_abort_enabled", lambda: True)
    monkeypatch.setattr(router.ttft_baselines, "deadline_s", lambda p, m: 1.5)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    health = []

    def fake_forward(provider, key, payload, streaming, model,
                     first_byte_deadline_s=None):
        if provider["name"] == "prov_a":
            assert first_byte_deadline_s == 1.5
            raise TtftDeadlineExceeded(1.5, 1.5)
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    monkeypatch.setattr(router.stats, "record_health",
                        lambda n, ok: health.append((n, ok)))
    router.sticky_store.set("sess1", provider="prov_a", model="m1", key="sk-a")

    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="ttft-cascade",
        _session_id="sess1",
        _sticky={"provider": "prov_a", "model": "m1", "key": "sk-a"},
    )
    assert result[0] == "json"
    fields = router._req_ctx.cascade.as_log_fields()
    assert any(s.get("reason") == "ttft_deadline" for s in fields["cascade"])
    assert fields["cascade"][-1]["outcome"] == "success"
    assert ("prov_a", False) not in health
    sticky = router.sticky_store.get("sess1")
    assert sticky is None or sticky.get("provider") == "prov_b"


def test_ttft_abort_disabled_passes_none_deadline(monkeypatch):
    ordered, a, b = _two_candidates()
    monkeypatch.setattr(router, "_ordered_providers", lambda *a, **k: ordered)
    _stub_common(monkeypatch)
    monkeypatch.setattr(router, "ttft_abort_enabled", lambda: False)
    monkeypatch.setattr(router.stats, "record_health", lambda *a, **k: None)

    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    seen = {}

    def fake_forward(provider, key, payload, streaming, model,
                     first_byte_deadline_s=None):
        seen["d"] = first_byte_deadline_s
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        resp.text = ""
        return resp

    monkeypatch.setattr(router, "forward", fake_forward)
    result = router._route_completion(
        {"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
        streaming=False, ns="ttft-disabled",
    )
    assert result[0] == "json"
    assert seen["d"] is None
