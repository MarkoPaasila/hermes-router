import time
from unittest.mock import MagicMock

import pytest
import requests

import router
from ttft_baseline import TtftBaselineStore, TtftDeadlineExceeded


def _provider():
    return {
        "name": "groq",
        "base_url": "https://example.test/v1",
        "model": "m",
        "headers": {},
    }


@pytest.fixture(autouse=True)
def _fresh_baselines(monkeypatch):
    store = TtftBaselineStore(
        floor_s=3.0, mult=3.0, min_samples=5, cold_deadline_s=20.0, alpha=1.0,
    )
    monkeypatch.setattr(router, "ttft_baselines", store)
    monkeypatch.setattr(router, "_model_caps", lambda *a, **k: {})
    monkeypatch.setattr(router, "_apply_output_token_cap", lambda *a, **k: None)
    return store


def test_forward_read_timeout_raises_ttft(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(router._HTTP, "post", boom)
    with pytest.raises(TtftDeadlineExceeded) as ei:
        router.forward(_provider(), "sk-test", {"messages": []}, False, "m",
                       first_byte_deadline_s=2.5)
    assert ei.value.deadline_s == 2.5
    assert router.ttft_baselines.summary("groq", "m")["sample_count"] == 0


def test_forward_success_records_ttft_and_extends(monkeypatch):
    resp = MagicMock()
    resp.status_code = 200
    called = {}

    def fake_post(*a, **k):
        called["timeout"] = k.get("timeout")
        time.sleep(0.01)
        return resp

    monkeypatch.setattr(router._HTTP, "post", fake_post)
    monkeypatch.setattr(
        router, "_extend_response_read_timeout",
        lambda r, s: called.setdefault("extended", s),
    )
    out = router.forward(_provider(), "sk-test", {"messages": []}, False, "m",
                         first_byte_deadline_s=5.0)
    assert out is resp
    assert called["timeout"] == (10, 5.0)
    assert called["extended"] == 180.0
    assert router.ttft_baselines.summary("groq", "m")["sample_count"] == 1


def test_forward_connect_timeout_returns_none(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectTimeout("nope")

    monkeypatch.setattr(router._HTTP, "post", boom)
    assert router.forward(
        _provider(), "sk", {"messages": []}, False, "m", first_byte_deadline_s=5.0,
    ) is None
    assert router.ttft_baselines.summary("groq", "m")["sample_count"] == 0
