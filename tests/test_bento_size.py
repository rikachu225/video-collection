"""Bento tile column spans persisted per theater clip."""
import json
import importlib


def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server


def _seed_theater(tmp_path, clips):
    (tmp_path / "theater.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")


def _clips():
    return [{"path": "Nature/river.mp4", "name": "river"},
            {"path": "Nature/forest.mp4", "name": "forest"}]


def test_size_persists_to_theater_json(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _clips())
    r = server.app.test_client().post("/api/theater/size",
                                      json={"path": "Nature/river.mp4", "cols": 12})
    assert r.status_code == 200
    assert r.get_json()["clips"][0]["bentoCols"] == 12
    saved = json.loads((tmp_path / "theater.json").read_text(encoding="utf-8"))
    assert saved["clips"][0]["bentoCols"] == 12


def test_size_clamped_to_range(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _clips())
    c = server.app.test_client()
    assert c.post("/api/theater/size", json={"path": "Nature/river.mp4", "cols": 99}
                  ).get_json()["clips"][0]["bentoCols"] == 12
    assert c.post("/api/theater/size", json={"path": "Nature/river.mp4", "cols": 0}
                  ).get_json()["clips"][0]["bentoCols"] == 2


def test_non_integer_cols_rejected(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _clips())
    c = server.app.test_client()
    assert c.post("/api/theater/size", json={"path": "Nature/river.mp4", "cols": "big"}).status_code == 400
    # bool is an int subclass in Python — must not slip through
    assert c.post("/api/theater/size", json={"path": "Nature/river.mp4", "cols": True}).status_code == 400


def test_unknown_path_returns_404(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _clips())
    r = server.app.test_client().post("/api/theater/size",
                                      json={"path": "Nature/ghost.mp4", "cols": 4})
    assert r.status_code == 404


def test_response_keeps_in_app_labels(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _clips())
    (tmp_path / "clip_names.json").write_text(
        json.dumps({"Nature/river.mp4": "Calm River"}), encoding="utf-8")
    clips = server.app.test_client().post(
        "/api/theater/size", json={"path": "Nature/river.mp4", "cols": 4}).get_json()["clips"]
    assert clips[0]["name"] == "Calm River"


def test_size_travels_into_saved_playlist(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _clips())
    c = server.app.test_client()
    theater = c.post("/api/theater/size", json={"path": "Nature/river.mp4", "cols": 4}).get_json()
    c.post("/api/playlists", json={"name": "Chill", "clips": theater["clips"]})
    saved = json.loads((tmp_path / "playlists.json").read_text(encoding="utf-8"))
    assert saved["playlists"][0]["clips"][0]["bentoCols"] == 4


# ── Folder layouts hold BOTH popup geometry and tile size, so saves must merge ──
def test_folder_layout_save_merges_tile_cols(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    c = server.app.test_client()
    c.post("/api/folder-layouts/0:Nature",
           json={"videoPath": "Nature/river.mp4",
                 "layout": {"left": 10, "top": 20, "width": 300, "height": 200}})
    c.post("/api/folder-layouts/0:Nature",
           json={"videoPath": "Nature/river.mp4", "layout": {"tileCols": 4}})
    entry = c.get("/api/folder-layouts/0:Nature").get_json()["Nature/river.mp4"]
    assert entry["tileCols"] == 4
    assert entry["left"] == 10 and entry["width"] == 300  # popup geometry survived


def test_popup_layout_save_does_not_wipe_tile_cols(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    c = server.app.test_client()
    c.post("/api/folder-layouts/0:Nature",
           json={"videoPath": "Nature/river.mp4", "layout": {"tileCols": 4}})
    c.post("/api/folder-layouts/0:Nature",
           json={"videoPath": "Nature/river.mp4",
                 "layout": {"left": 10, "top": 20, "width": 300, "height": 200}})
    entry = c.get("/api/folder-layouts/0:Nature").get_json()["Nature/river.mp4"]
    assert entry["tileCols"] == 4  # tile size survived
    assert entry["left"] == 10
