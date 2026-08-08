# tests/test_model_pinning.py
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
