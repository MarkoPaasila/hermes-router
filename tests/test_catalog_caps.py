import router


def test_catalog_caps_none_for_missing_item():
    assert router._catalog_caps_from_item("openrouter", None) == {
        "supports_tools": None, "reasoning": None,
    }


def test_catalog_caps_openrouter_tools_and_reasoning():
    item = {
        "id": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "supported_parameters": [
            "include_reasoning", "max_tokens", "reasoning", "reasoning_effort",
            "seed", "temperature", "tool_choice", "tools", "top_p",
        ],
        "reasoning": {
            "mandatory": False, "default_enabled": True,
            "supports_max_tokens": True,
            "supported_efforts": ["high", "medium"], "default_effort": "high",
        },
    }
    assert router._catalog_caps_from_item("openrouter", item) == {
        "supports_tools": True, "reasoning": True,
    }


def test_catalog_caps_openrouter_rich_params_without_tools():
    item = {
        "id": "nvidia/nemotron-3.5-content-safety:free",
        "supported_parameters": [
            "include_reasoning", "max_tokens", "reasoning", "temperature", "top_p",
        ],
        "reasoning": {"mandatory": False},
    }
    caps = router._catalog_caps_from_item("openrouter", item)
    assert caps["supports_tools"] is False
    assert caps["reasoning"] is True


def test_catalog_caps_thin_item_is_silent():
    item = {"id": "some-model", "object": "model"}
    assert router._catalog_caps_from_item("opencode", item) == {
        "supports_tools": None, "reasoning": None,
    }


def test_catalog_caps_default_adapter_never_asserts_false():
    """Even if params look rich, unknown providers stay silent on false."""
    item = {
        "id": "x",
        "supported_parameters": ["temperature", "max_tokens"],
    }
    caps = router._catalog_caps_from_item("opencode", item)
    assert caps["supports_tools"] is None
    assert caps["reasoning"] is None


def test_catalog_caps_gemini_thinking_true():
    caps = router._catalog_caps_from_item(
        "gemini", {"id": "gemini-2.5-flash", "thinking": True})
    assert caps["reasoning"] is True
    assert caps["supports_tools"] is None


def test_catalog_caps_gemini_thinking_false():
    caps = router._catalog_caps_from_item(
        "gemini", {"id": "gemini-2.0-flash", "thinking": False})
    assert caps["reasoning"] is False


def test_catalog_caps_gemini_null_thinking_is_silent():
    caps = router._catalog_caps_from_item(
        "gemini", {"id": "antigravity-preview-05-2026", "thinking": None})
    assert caps["reasoning"] is None


def test_catalog_caps_gemini_without_thinking_is_silent():
    caps = router._catalog_caps_from_item(
        "gemini", {"id": "gemini-2.0-flash", "object": "model"})
    assert caps["reasoning"] is None


def test_shared_reasoning_hint_matches_normalized_ids(monkeypatch):
    monkeypatch.setattr(
        router, "_load_openrouter_reasoning_index",
        lambda: {
            "deepseek-v3.2": True,
            "deepseek-v3.1": True,  # from terminus strip
            "minimax-m2.7": True,
            "gemma-4-31b-it": True,
            "gemma-4-31b": True,
            "gpt-oss-120b": True,
        })
    assert router._shared_reasoning_hint("DeepSeek-V3.2") is True
    assert router._shared_reasoning_hint("DeepSeek-V3.1") is True
    assert router._shared_reasoning_hint("MiniMax-M2.7") is True
    assert router._shared_reasoning_hint("gemma-4-31B-it") is True
    assert router._shared_reasoning_hint("gemma-4-31b") is True
    assert router._shared_reasoning_hint("gpt-oss-120b") is True
    assert router._shared_reasoning_hint("antigravity-preview-05-2026") is None


def test_resolve_caps_uses_shared_hint_before_probe(monkeypatch):
    probed = {"reasoning": 0}

    def fake_reasoning(*a, **k):
        probed["reasoning"] += 1
        return False

    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: True)
    monkeypatch.setattr(router, "_probe_reasoning", fake_reasoning)
    monkeypatch.setattr(router, "_shared_reasoning_hint", lambda m: True)
    p = {"name": "sambanova", "base_url": "https://example/v1", "headers": {}}
    caps = router._resolve_caps(
        p, "sk", "DeepSeek-V3.2", True,
        catalog_item={"id": "DeepSeek-V3.2"}, prior={"reasoning": False, "reasoning_source": "probe"})
    assert caps["reasoning"] is True
    assert caps["reasoning_source"] == "catalog"
    assert probed["reasoning"] == 0


def test_reasoning_index_keys_strips_it_suffix():
    keys = router._reasoning_index_keys("google/gemma-4-31b-it")
    assert "gemma-4-31b-it" in keys
    assert "gemma-4-31b" in keys



def test_merge_capability_keeps_sticky_true_against_probe_false():
    prior = {"supports_tools": True, "supports_tools_source": "catalog"}
    val, src = router._merge_capability(prior, "supports_tools", False, "probe")
    assert val is True
    assert src == "catalog"


def test_merge_capability_upgrades_false_to_catalog_true():
    prior = {"supports_tools": False, "supports_tools_source": "probe"}
    val, src = router._merge_capability(prior, "supports_tools", True, "catalog")
    assert val is True
    assert src == "catalog"


def test_merge_capability_accepts_probe_false_when_no_sticky():
    prior = None
    val, src = router._merge_capability(prior, "reasoning", False, "probe")
    assert val is False
    assert src == "probe"


def test_resolve_caps_uses_catalog_and_skips_probes(monkeypatch):
    probed = {"tools": 0, "reasoning": 0}

    def fake_tools(*a, **k):
        probed["tools"] += 1
        return False

    def fake_reasoning(*a, **k):
        probed["reasoning"] += 1
        return False

    monkeypatch.setattr(router, "_probe_tools", fake_tools)
    monkeypatch.setattr(router, "_probe_reasoning", fake_reasoning)

    item = {
        "id": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "supported_parameters": ["tools", "tool_choice", "reasoning", "max_tokens"],
        "reasoning": {"mandatory": False},
    }
    p = {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "headers": {}}
    caps = router._resolve_caps(
        p, "sk", "nvidia/nemotron-3-ultra-550b-a55b:free", True,
        catalog_item=item, prior=None)
    assert caps["supports_tools"] is True
    assert caps["reasoning"] is True
    assert caps["supports_tools_source"] == "catalog"
    assert caps["reasoning_source"] == "catalog"
    assert probed == {"tools": 0, "reasoning": 0}


def test_resolve_caps_gemini_thinking_skips_probe(monkeypatch):
    probed = {"reasoning": 0}

    def fake_reasoning(*a, **k):
        probed["reasoning"] += 1
        return False

    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: True)
    monkeypatch.setattr(router, "_probe_reasoning", fake_reasoning)
    p = {"name": "gemini", "base_url": "https://example/v1", "headers": {}}
    caps = router._resolve_caps(
        p, "sk", "gemini-2.5-flash", True,
        catalog_item={"id": "gemini-2.5-flash", "thinking": True},
        prior={"supports_tools": True, "reasoning": False, "reasoning_source": "probe"})
    assert caps["reasoning"] is True
    assert caps["reasoning_source"] == "catalog"
    assert probed["reasoning"] == 0


def test_resolve_caps_probes_when_catalog_silent(monkeypatch):
    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: True)
    monkeypatch.setattr(router, "_probe_reasoning", lambda *a, **k: False)
    p = {"name": "opencode", "base_url": "https://example/v1", "headers": {}}
    caps = router._resolve_caps(
        p, "sk", "big-pickle", True,
        catalog_item={"id": "big-pickle"}, prior=None)
    assert caps["supports_tools"] is True
    assert caps["supports_tools_source"] == "probe"
    assert caps["reasoning"] is False
    assert caps["reasoning_source"] == "probe"


def test_resolve_caps_reprobes_sticky_probe_false(monkeypatch):
    """OpenCode-style: thin catalog + prior probe-false must re-probe."""
    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: True)
    monkeypatch.setattr(router, "_probe_reasoning", lambda *a, **k: True)
    p = {"name": "opencode", "base_url": "https://example/v1", "headers": {}}
    prior = {
        "supports_tools": True, "supports_tools_source": "probe",
        "reasoning": False, "reasoning_source": "probe",
    }
    caps = router._resolve_caps(
        p, "sk", "nemotron-3-ultra-free", True,
        catalog_item={"id": "nemotron-3-ultra-free"}, prior=prior)
    assert caps["reasoning"] is True
    assert caps["reasoning_source"] == "probe"


def test_resolve_caps_sticky_survives_probe_false(monkeypatch):
    monkeypatch.setattr(router, "_probe_tools", lambda *a, **k: False)
    monkeypatch.setattr(router, "_probe_reasoning", lambda *a, **k: False)
    p = {"name": "opencode", "base_url": "https://example/v1", "headers": {}}
    prior = {
        "supports_tools": True, "supports_tools_source": "catalog",
        "reasoning": True, "reasoning_source": "catalog",
    }
    caps = router._resolve_caps(
        p, "sk", "nemotron-3-ultra-free", True,
        catalog_item={"id": "nemotron-3-ultra-free"}, prior=prior)
    assert caps["supports_tools"] is True
    assert caps["supports_tools_source"] == "catalog"
    assert caps["reasoning"] is True
    assert caps["reasoning_source"] == "catalog"


def test_resolve_caps_env_override_wins(monkeypatch):
    monkeypatch.setenv(
        "OPENROUTER_NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_FREE_SUPPORTS_TOOLS", "0")
    monkeypatch.setenv(
        "OPENROUTER_NVIDIA_NEMOTRON_3_ULTRA_550B_A55B_FREE_REASONING", "0")
    item = {
        "supported_parameters": ["tools", "reasoning"],
        "reasoning": {},
    }
    p = {"name": "openrouter", "base_url": "https://openrouter.ai/api/v1", "headers": {}}
    caps = router._resolve_caps(
        p, "sk", "nvidia/nemotron-3-ultra-550b-a55b:free", True,
        catalog_item=item,
        prior={"supports_tools": True, "supports_tools_source": "catalog",
               "reasoning": True, "reasoning_source": "catalog"},
    )
    assert caps["supports_tools"] is False
    assert caps["reasoning"] is False


def test_fetch_models_catalog_map_normalizes_ids(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [
                {"id": "models/gemini-2.5-flash", "supported_parameters": ["tools"]},
                {"id": "  other-model  "},
            ]}

    monkeypatch.setattr(router._HTTP, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(router, "_enrich_gemini_catalog_thinking", lambda out, key: None)
    p = {"name": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
         "headers": {}}
    m = router._fetch_models_catalog_map(p, "sk")
    assert "gemini-2.5-flash" in m
    assert "other-model" in m
    assert m["gemini-2.5-flash"]["id"] == "models/gemini-2.5-flash"
