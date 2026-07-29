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
