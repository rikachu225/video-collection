import json
import importlib


def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server


def test_config_get_reports_not_configured(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    c = server.app.test_client()
    r = c.get("/api/ai/config")
    assert r.status_code == 200
    body = r.get_json()
    assert body["configured"] is False
    assert "geminiApiKey" not in body  # never leak the key field


def test_config_post_then_get_never_returns_key(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    c = server.app.test_client()
    c.post("/api/ai/config", json={"geminiApiKey": "SECRET123456", "enabled": True})
    r = c.get("/api/ai/config")
    body = r.get_json()
    assert body["configured"] is True
    assert "SECRET123456" not in json.dumps(body)


def test_env_var_takes_precedence(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "ENVKEY")
    c = server.app.test_client()
    r = c.get("/api/ai/config")
    assert r.get_json()["configured"] is True


def test_agent_503_when_not_configured(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    c = server.app.test_client()
    r = c.post("/api/agent", json={"message": "hi", "history": [], "context": {}})
    assert r.status_code == 503
    assert r.get_json().get("needs_setup") is True


def test_agent_happy_path_mocked(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "ENVKEY")
    import ai_agent
    monkeypatch.setattr(ai_agent, "run_agent",
                        lambda *a, **k: {"reply": "ok", "ui_commands": [{"command": "play_all", "args": {}}], "refresh": ["theater"]})
    c = server.app.test_client()
    r = c.post("/api/agent", json={"message": "play all", "history": [], "context": {}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["reply"] == "ok"
    assert body["ui_commands"][0]["command"] == "play_all"
