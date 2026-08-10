"""Free-only discovery must not prune configured free models like big-pickle."""
import router


class _FakeModelsResp:
    def __init__(self, data, status_code=200):
        self.status_code = status_code
        self._data = data

    def json(self):
        return {"data": self._data}


class _FakePool:
    def ensure_model(self, name, model, keys):
        pass

    def rename_model(self, old, new, model):
        pass


def test_is_free_model_id_recognizes_big_pickle():
    assert router._is_free_model_id("big-pickle") is True
    assert router._is_free_model_id("deepseek-v4-flash-free") is True
    assert router._is_free_model_id("claude-sonnet-4-5") is False


def test_free_only_catalog_keeps_non_suffix_for_membership(monkeypatch):
    """Membership catalog retains big-pickle; append list includes it as free."""
    catalog = [
        {"id": "big-pickle"},
        {"id": "deepseek-v4-flash-free"},
        {"id": "claude-sonnet-4-5"},
        {"id": "nemotron-3-ultra-free"},
    ]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    provider = {
        "name": "opencode",
        "base_url": "https://opencode.ai/zen/v1",
        "headers": {},
        "model": "big-pickle",
        "models": ["big-pickle"],
        "keys": ["sk-test"],
    }
    filtered, full = router._discover_models_with_catalog(
        provider, "sk-test", free_only=True)
    assert "big-pickle" in full
    assert "claude-sonnet-4-5" in full  # membership only
    assert "big-pickle" in filtered
    assert "deepseek-v4-flash-free" in filtered
    assert "claude-sonnet-4-5" not in filtered


def test_refresh_keeps_configured_big_pickle_under_free_only(monkeypatch):
    monkeypatch.setattr(router, "AUTO_DISCOVER_MODELS", True)
    monkeypatch.setattr(router, "AUTO_DISCOVER_MODEL_LIMIT", 8)
    monkeypatch.delenv("OPENCODE_AUTO_DISCOVER_MODELS", raising=False)
    catalog = [
        {"id": "big-pickle"},
        {"id": "deepseek-v4-flash-free"},
        {"id": "nemotron-3-ultra-free"},
        {"id": "mimo-v2.5-free"},
        {"id": "claude-sonnet-4-5"},
        {"id": "gpt-5.5"},
    ]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    provider = {
        "name": "opencode",
        "base_url": "https://opencode.ai/zen/v1",
        "headers": {},
        "model": "big-pickle",
        "models": ["big-pickle", "deepseek-v4-flash-free"],
        "keys": ["sk-test"],
    }
    router._refresh_discovered_models(provider, "sk-test", _FakePool())
    assert "big-pickle" in provider["models"]
    assert "deepseek-v4-flash-free" in provider["models"]
    assert "claude-sonnet-4-5" not in provider["models"]
    assert "gpt-5.5" not in provider["models"]
