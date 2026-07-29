"""Router wiring tests for adaptive token caps."""
import router
from token_caps import TokenCapTracker


def test_skip_uses_effective_input_cap_per_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("groq", "small", max_input=1000)
    monkeypatch.setattr(router, "token_caps", caps)

    provider = {"name": "groq", "skip_if_tokens_over": 5500}
    assert router._effective_input_cap_for(provider, "small") == 1000
    assert router._effective_input_cap_for(provider, "large") == 5500


def test_forward_clamp_uses_effective_output_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("cohere", "command-a", max_output=2048)
    monkeypatch.setattr(router, "token_caps", caps)

    provider = {
        "name": "cohere",
        "model": "command-a",
        "max_output_tokens": 8192,
    }
    body = {"model": "command-a", "max_tokens": 65536, "messages": []}
    router._apply_output_token_cap(body, provider, "command-a")
    assert body["max_tokens"] == 2048


def test_token_caps_disabled_uses_env_only(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=False)
    caps._caps[("groq", "llama")] = {
        "max_input": 100, "max_output": 50, "source": "learned", "updated_at": 0,
    }
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", False)
    provider = {"name": "groq", "skip_if_tokens_over": 5500, "max_output_tokens": 8192}
    assert router._effective_input_cap_for(provider, "llama") == 5500
    body = {"max_tokens": 65536}
    router._apply_output_token_cap(body, {**provider, "model": "llama"}, "llama")
    assert body["max_tokens"] == 8192


def test_learn_from_classified_413(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    router._learn_token_cap_from_error(
        provider_name="groq",
        model="llama",
        status_code=413,
        body="Payload Too Large",
        est_tokens=6000,
        requested_max_tokens=1024,
    )
    assert caps.effective_input_cap("groq", "llama", 0) is not None
    assert caps.effective_input_cap("groq", "llama", 0) < 6000


def test_unrelated_400_does_not_learn(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    monkeypatch.setattr(router, "token_caps", caps)
    router._learn_token_cap_from_error(
        provider_name="groq",
        model="llama",
        status_code=400,
        body="invalid tool schema",
        est_tokens=6000,
        requested_max_tokens=1024,
    )
    assert caps.snapshot("groq", "llama") is None


def test_discover_seeds_context_length(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    monkeypatch.setattr(router, "FILTER_SPECIALIZED_MODELS", False)

    catalog = [
        {
            "id": "llama-3.3-70b-versatile",
            "context_length": 8192,
            "max_completion_tokens": 4096,
        },
        {"id": "whisper-1"},
    ]

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": catalog}

    monkeypatch.setattr(router._HTTP, "get", lambda *a, **k: _Resp())
    provider = {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "headers": {},
    }
    found = router._discover_models(provider, key="sk-test")
    assert "llama-3.3-70b-versatile" in found
    assert caps.effective_input_cap("groq", "llama-3.3-70b-versatile", 0) == 8192
    assert caps.effective_output_cap("groq", "llama-3.3-70b-versatile", 0) == 4096


def test_success_near_cap_nudge_helper(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("groq", "llama", max_input=1000)
    monkeypatch.setattr(router, "token_caps", caps)
    router._learn_token_cap_from_success(
        provider_name="groq",
        model="llama",
        prompt_tokens=900,
        completion_tokens=10,
        provider={"name": "groq", "skip_if_tokens_over": 5500},
    )
    assert caps.effective_input_cap("groq", "llama", 0) > 1000


def test_learn_output_error_uses_clamped_max_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("cohere", "command-a", max_output=2048)
    monkeypatch.setattr(router, "token_caps", caps)

    provider = {
        "name": "cohere",
        "model": "command-a",
        "max_output_tokens": 8192,
    }
    payload = {"max_tokens": 65536}
    req_max = router._effective_requested_output_for_learning(provider, "command-a", payload)
    assert req_max == 2048

    router._learn_token_cap_from_error(
        provider_name="cohere",
        model="command-a",
        status_code=400,
        body="max_tokens is too large: 65536",
        est_tokens=100,
        requested_max_tokens=req_max,
    )
    learned = caps.effective_output_cap("cohere", "command-a", 8192)
    assert learned is not None
    assert learned < 2048


def test_success_near_cap_respects_env_bound(monkeypatch, tmp_path):
    caps = TokenCapTracker(state_file=tmp_path / "c.json", enabled=True)
    caps.seed_from_metadata("groq", "llama", max_input=8192)
    monkeypatch.setattr(router, "token_caps", caps)
    monkeypatch.setattr(router, "TOKEN_CAPS_ENABLED", True)
    router._learn_token_cap_from_success(
        provider_name="groq",
        model="llama",
        prompt_tokens=5000,
        completion_tokens=None,
        provider={"name": "groq", "skip_if_tokens_over": 5500},
    )
    assert caps.effective_input_cap("groq", "llama", 0) == int(8192 * 1.05)
    assert caps.effective_input_cap("groq", "llama", 5500) == 5500
