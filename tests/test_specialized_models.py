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
