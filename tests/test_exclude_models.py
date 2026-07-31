"""Tests for dashboard Block/Unblock via {PROVIDER}_EXCLUDE_MODELS."""
import os

import pytest

import router


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("# test env\n")
    monkeypatch.setattr(router, "ENV_FILE_PATH", path)
    return path


@pytest.fixture
def clean_exclude(monkeypatch, env_file):
    """Clear OPENROUTER_EXCLUDE_MODELS and restore provider roster after test."""
    key = "OPENROUTER_EXCLUDE_MODELS"
    monkeypatch.delenv(key, raising=False)
    prov = next(p for p in router.PROVIDERS if p["name"] == "openrouter")
    original_models = list(prov.get("models") or [])
    original_model = prov.get("model", "")
    yield prov
    monkeypatch.delenv(key, raising=False)
    if env_file.exists():
        lines = [ln for ln in env_file.read_text().splitlines()
                 if not ln.startswith(f"{key}=")]
        env_file.write_text("\n".join(lines) + ("\n" if lines else ""))
    prov["models"] = list(original_models)
    prov["model"] = original_model


def test_exclude_list_add_remove_dedupe_and_case(env_file, monkeypatch):
    monkeypatch.delenv("MISTRAL_EXCLUDE_MODELS", raising=False)
    items = router._exclude_list_add("mistral", "mistral-tiny")
    assert items == ["mistral-tiny"]
    assert os.environ["MISTRAL_EXCLUDE_MODELS"] == "mistral-tiny"
    assert "MISTRAL_EXCLUDE_MODELS=mistral-tiny" in env_file.read_text()

    # Case-insensitive dedupe keeps original casing
    items = router._exclude_list_add("mistral", "Mistral-Tiny")
    assert items == ["mistral-tiny"]

    router._exclude_list_add("mistral", "other-model")
    assert router._exclude_list_raw("mistral") == ["mistral-tiny", "other-model"]

    left = router._exclude_list_remove("mistral", "MISTRAL-TINY")
    assert left == ["other-model"]

    left = router._exclude_list_remove("mistral", "other-model")
    assert left == []
    assert "MISTRAL_EXCLUDE_MODELS" not in os.environ
    assert "MISTRAL_EXCLUDE_MODELS=" not in env_file.read_text()


def test_set_model_excluded_removes_from_roster(clean_exclude, env_file):
    prov = clean_exclude
    # Ensure at least two models so primary can shift
    if "keep-me" not in [m.lower() for m in prov["models"]]:
        prov["models"] = list(prov["models"]) + ["keep-me"]
    if not prov["models"]:
        prov["models"] = ["block-me", "keep-me"]
    target = prov["models"][0]
    rest = [m for m in prov["models"] if m.lower() != target.lower()]
    if not rest:
        prov["models"].append("keep-me")
        rest = ["keep-me"]

    excluded = router._set_model_excluded("openrouter", target, True)
    assert target.lower() in {m.lower() for m in excluded}
    assert all(m.lower() != target.lower() for m in prov["models"])
    assert prov["model"] == (prov["models"][0] if prov["models"] else "")
    assert "OPENROUTER_EXCLUDE_MODELS=" in env_file.read_text()

    excluded = router._set_model_excluded("openrouter", target, False)
    assert all(m.lower() != target.lower() for m in excluded)
    assert any(m.lower() == target.lower() for m in prov["models"])
    assert target in router.pool.pools.get("openrouter", {})


def test_idempotent_block_and_unblock(clean_exclude):
    prov = clean_exclude
    mid = "idempotent-model-xyz"
    if not any(m.lower() == mid for m in prov["models"]):
        prov["models"] = list(prov["models"]) + [mid]

    router._set_model_excluded("openrouter", mid, True)
    router._set_model_excluded("openrouter", mid, True)
    assert router._exclude_list_raw("openrouter").count(mid) == 1
    assert all(m.lower() != mid for m in prov["models"])

    router._set_model_excluded("openrouter", mid, False)
    router._set_model_excluded("openrouter", mid, False)
    assert mid not in [m.lower() for m in router._exclude_list_raw("openrouter")]
    assert any(m.lower() == mid for m in prov["models"])


def test_all_excluded_models_preserves_casing(monkeypatch, env_file):
    monkeypatch.setenv("GROQ_EXCLUDE_MODELS", "Llama-3.3,other/Model:free")
    rows = router._all_excluded_models()
    groq = [r for r in rows if r["provider"] == "groq"]
    assert {"provider": "groq", "model": "Llama-3.3"} in groq
    assert {"provider": "groq", "model": "other/Model:free"} in groq
    monkeypatch.delenv("GROQ_EXCLUDE_MODELS", raising=False)


def _auth_headers():
    return {"Authorization": f"Bearer {router.PROXY_API_KEYS[0]}"}


def test_api_exclude_model_block_unblock(clean_exclude, env_file):
    prov = clean_exclude
    mid = "api-block-model"
    if not any(m.lower() == mid for m in (prov.get("models") or [])):
        prov["models"] = list(prov.get("models") or []) + [mid]
        router.pool.ensure_model("openrouter", mid, list(prov.get("keys") or ["k"]))

    client = router.app.test_client()
    r = client.post(
        "/v1/config/exclude-model",
        json={"provider": "openrouter", "model": mid, "blocked": True},
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["blocked"] is True
    assert mid in body["excluded"]
    assert all(m.lower() != mid for m in prov["models"])

    r = client.get("/v1/config/excluded-models", headers=_auth_headers())
    assert r.status_code == 200
    assert any(e["provider"] == "openrouter" and e["model"] == mid
               for e in r.get_json()["excluded"])

    r = client.post(
        "/v1/config/exclude-model",
        json={"provider": "openrouter", "model": mid, "blocked": False},
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    assert mid not in r.get_json()["excluded"]
    assert any(m.lower() == mid for m in prov["models"])


def test_api_exclude_model_validation(env_file):
    client = router.app.test_client()
    r = client.post(
        "/v1/config/exclude-model",
        json={"provider": "nope", "model": "x", "blocked": True},
        headers=_auth_headers(),
    )
    assert r.status_code == 400

    r = client.post(
        "/v1/config/exclude-model",
        json={"provider": "openrouter", "blocked": True},
        headers=_auth_headers(),
    )
    assert r.status_code == 400


def test_blocked_model_not_in_status_models(clean_exclude):
    """After block, /v1/status must not advertise the model on the provider."""
    prov = clean_exclude
    mid = "status-hide-model"
    prov["models"] = list(dict.fromkeys(list(prov.get("models") or []) + [mid]))
    router.pool.ensure_model("openrouter", mid, list(prov.get("keys") or ["k"]))

    router._set_model_excluded("openrouter", mid, True)
    client = router.app.test_client()
    r = client.get("/v1/status", headers=_auth_headers())
    assert r.status_code == 200
    models = r.get_json()["providers"]["openrouter"].get("models") or []
    assert all(m.lower() != mid for m in models)

    router._set_model_excluded("openrouter", mid, False)
