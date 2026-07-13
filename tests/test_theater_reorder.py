import json
import importlib


def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server


def _seed(tmp_path, clips):
    (tmp_path / "theater.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")


def test_reorder_rewrites_and_persists_order(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed(tmp_path, [{"path": p, "name": p} for p in ["a", "b", "c"]])
    c = server.app.test_client()
    r = c.post("/api/theater/reorder", json={"paths": ["c", "a", "b"]})
    assert r.status_code == 200
    assert [clip["path"] for clip in r.get_json()["clips"]] == ["c", "a", "b"]
    saved = json.loads((tmp_path / "theater.json").read_text(encoding="utf-8"))
    assert [clip["path"] for clip in saved["clips"]] == ["c", "a", "b"]


def test_reorder_preserves_full_clip_objects(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed(tmp_path, [
        {"path": "a", "name": "Alpha", "loopStart": 5, "loopEnd": 10},
        {"path": "b", "name": "Bravo"},
    ])
    c = server.app.test_client()
    clips = c.post("/api/theater/reorder", json={"paths": ["b", "a"]}).get_json()["clips"]
    assert clips[0]["path"] == "b"
    assert clips[1]["name"] == "Alpha" and clips[1]["loopStart"] == 5  # data, not just paths


def test_reorder_ignores_unknown_and_appends_omitted(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed(tmp_path, [{"path": p, "name": p} for p in ["a", "b", "c"]])
    c = server.app.test_client()
    # 'z' doesn't exist (ignored); 'c' omitted from the list → kept at the end
    r = c.post("/api/theater/reorder", json={"paths": ["b", "z", "a"]})
    assert [clip["path"] for clip in r.get_json()["clips"]] == ["b", "a", "c"]
