"""DASHBOARD_OPEN auth bypass for dashboard-backing routes."""
import router


def _auth_headers():
    return {"Authorization": f"Bearer {router.PROXY_API_KEYS[0]}"}


def test_closed_status_and_config_require_key(monkeypatch):
    monkeypatch.setattr(router, "DASHBOARD_OPEN", False)
    client = router.app.test_client()
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/config/providers").status_code == 401
    r = client.get("/v1/status", headers=_auth_headers())
    assert r.status_code == 200
    r = client.get("/v1/config/providers", headers=_auth_headers())
    assert r.status_code == 200


def test_open_allows_dashboard_routes_without_key(monkeypatch):
    monkeypatch.setattr(router, "DASHBOARD_OPEN", True)
    client = router.app.test_client()
    for path in (
        "/v1/status",
        "/v1/usage",
        "/v1/capacity",
        "/v1/rate-limits",
        "/v1/logs",
        "/v1/config/providers",
        "/v1/config/proxy-keys",
        "/v1/config/excluded-models",
    ):
        r = client.get(path)
        assert r.status_code != 401, path
        assert r.status_code == 200, (path, r.status_code, r.get_data(as_text=True)[:200])


def test_open_still_keys_chat_and_models(monkeypatch):
    monkeypatch.setattr(router, "DASHBOARD_OPEN", True)
    client = router.app.test_client()
    assert client.get("/v1/models").status_code == 401
    assert client.post(
        "/v1/chat/completions",
        json={"model": "hermes-router", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 401
    assert client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "hi"},
    ).status_code == 401


def test_dashboard_open_feature_addon(monkeypatch):
    monkeypatch.setattr(router, "DASHBOARD_OPEN", True)
    snap = router._features_snapshot()
    addon = next(a for a in snap["addons"] if a["name"] == "dashboard_open")
    assert addon["kind"] == "flag"
    assert addon["env"] == "DASHBOARD_OPEN"
    assert addon["on"] == "1"
    assert addon["off"] == "0"
    assert addon["enabled"] is True

    monkeypatch.setattr(router, "DASHBOARD_OPEN", False)
    snap = router._features_snapshot()
    addon = next(a for a in snap["addons"] if a["name"] == "dashboard_open")
    assert addon["enabled"] is False


def test_dashboard_html_keeps_key_gate():
    client = router.app.test_client()
    r = client.get("/dashboard")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="key-gate"' in html
    assert "dashHeaders" in html
    assert "DASHBOARD_OPEN" in html or "open mode" in html.lower()
