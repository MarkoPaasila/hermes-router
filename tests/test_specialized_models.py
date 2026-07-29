"""Tests for specialized-model detection and discovery filtering."""
import router


def test_name_denylist_drops_specialized_ids():
    for mid in (
        "whisper-1",
        "tts-1",
        "gpt-4o-audio-preview",
        "google/imagen-3",
        "dall-e-3",
        "black-forest-labs/flux-schnell",
        "ocr-model",
        "text-embedding-3-small",
        "gemini-embedding-001",
        "text-moderation-latest",
        "rerank-v3.5",
        "sora-video-preview",
    ):
        assert router._is_specialized_model(mid) is True, mid


def test_name_keeps_chat_and_vision_chat_ids():
    for mid in (
        "gpt-4o",
        "claude-sonnet-4",
        "gemini-2.5-flash",
        "llama-3.3-70b-instruct",
        "pixtral-12b",
        "gpt-4o-mini",
    ):
        assert router._is_specialized_model(mid) is False, mid


def test_metadata_specialized_drops_even_with_chatty_name():
    item = {
        "id": "custom-helper",
        "architecture": {"modality": "text->embedding", "output_modalities": ["embeddings"]},
    }
    assert router._is_specialized_model("custom-helper", item) is True


def test_metadata_chat_keeps_even_if_name_has_audio_token():
    # Explicit chat/text metadata wins over name denylist.
    item = {
        "id": "vendor-audio-chat-7b",
        "architecture": {"modality": "text+image->text", "output_modalities": ["text"]},
    }
    assert router._is_specialized_model("vendor-audio-chat-7b", item) is False


def test_openrouter_image_generation_modality_drops():
    item = {
        "id": "vendor/gen-model",
        "architecture": {"modality": "text->image", "output_modalities": ["image"]},
    }
    assert router._is_specialized_model("vendor/gen-model", item) is True


def test_unclear_metadata_falls_back_to_name():
    item = {"id": "whisper-large-v3", "architecture": {}}
    assert router._is_specialized_model("whisper-large-v3", item) is True


class _FakeModelsResp:
    def __init__(self, models, status_code=200):
        self.status_code = status_code
        self._models = models

    def json(self):
        return {"data": self._models}


def test_discover_models_filters_specialized_when_flag_on(monkeypatch):
    monkeypatch.setattr(router, "FILTER_SPECIALIZED_MODELS", True)
    catalog = [
        {"id": "gpt-4o-mini"},
        {"id": "whisper-1"},
        {"id": "text-embedding-3-small"},
        {"id": "dall-e-3"},
        {"id": "claude-sonnet-4"},
        {
            "id": "vendor/gen",
            "architecture": {"modality": "text->image", "output_modalities": ["image"]},
        },
    ]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    provider = {"name": "openai", "base_url": "https://api.openai.com/v1", "headers": {}}
    found = router._discover_models(provider, key="sk-test")
    assert "gpt-4o-mini" in found
    assert "claude-sonnet-4" in found
    assert "whisper-1" not in found
    assert "text-embedding-3-small" not in found
    assert "dall-e-3" not in found
    assert "vendor/gen" not in found


def test_discover_models_keeps_specialized_when_flag_off(monkeypatch):
    monkeypatch.setattr(router, "FILTER_SPECIALIZED_MODELS", False)
    catalog = [
        {"id": "gpt-4o-mini"},
        {"id": "whisper-1"},
        {"id": "text-embedding-3-small"},
    ]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    provider = {"name": "openai", "base_url": "https://api.openai.com/v1", "headers": {}}
    found = router._discover_models(provider, key="sk-test")
    assert set(found) == {"gpt-4o-mini", "whisper-1", "text-embedding-3-small"}


def test_discover_best_model_skips_specialized_when_flag_on(monkeypatch):
    monkeypatch.setattr(router, "FILTER_SPECIALIZED_MODELS", True)
    catalog = [
        {"id": "whisper-1"},
        {"id": "gpt-4o-mini"},
        {"id": "text-embedding-3-small"},
    ]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    best = router._discover_best_model("https://api.openai.com/v1", key="sk-test")
    assert best == "gpt-4o-mini"


def test_discover_best_model_all_specialized_returns_none(monkeypatch):
    monkeypatch.setattr(router, "FILTER_SPECIALIZED_MODELS", True)
    catalog = [{"id": "whisper-1"}, {"id": "dall-e-3"}]

    def fake_get(url, headers=None, timeout=None):
        return _FakeModelsResp(catalog)

    monkeypatch.setattr(router._HTTP, "get", fake_get)
    assert router._discover_best_model("https://api.openai.com/v1", key="sk-test") is None
