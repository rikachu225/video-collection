"""In-app clip display names (labels).

Renaming a clip stores a display label keyed by the clip's path in
clip_names.json — the file on disk is NEVER touched. The label overrides the
`name` field wherever a clip is listed (browse, theater, playlists).
"""
import json
import importlib


def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server


def _media_root(tmp_path, folder="Nature", filename="river.mp4"):
    """Create a media root with one folder + video file; wire it into config.json."""
    root = tmp_path / "media"
    (root / folder).mkdir(parents=True, exist_ok=True)
    (root / folder / filename).write_bytes(b"\x00")
    (tmp_path / "config.json").write_text(
        json.dumps({"mediaPaths": [{"path": str(root), "name": "Videos"}], "excludedFolders": []}),
        encoding="utf-8",
    )
    return root


def _seed_clip_names(tmp_path, mapping):
    (tmp_path / "clip_names.json").write_text(json.dumps(mapping), encoding="utf-8")


def test_set_name_reflected_in_browse(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    r = c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "Calm River"})
    assert r.status_code == 200
    assert r.get_json()["name"] == "Calm River"
    vids = c.get("/api/videos/Nature").get_json()
    assert vids[0]["name"] == "Calm River"
    assert vids[0]["path"] == "Nature/river.mp4"  # identity (path) unchanged


def test_disk_file_is_never_renamed(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    root = _media_root(tmp_path)
    c = server.app.test_client()
    c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "Calm River"})
    # The actual file on disk keeps its original name — this is the whole point of label-only.
    assert (root / "Nature" / "river.mp4").exists()
    assert not (root / "Nature" / "Calm River.mp4").exists()


def test_empty_name_reverts_to_stem(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "Calm River"})
    r = c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "   "})
    assert r.status_code == 200
    assert r.get_json()["name"] == "river"  # back to the filename stem
    vids = c.get("/api/videos/Nature").get_json()
    assert vids[0]["name"] == "river"
    saved = json.loads((tmp_path / "clip_names.json").read_text(encoding="utf-8"))
    assert "Nature/river.mp4" not in saved  # override entry removed


def test_override_applied_to_theater(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    (tmp_path / "theater.json").write_text(
        json.dumps({"clips": [{"path": "Nature/river.mp4", "name": "river"}]}), encoding="utf-8"
    )
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    clips = c.get("/api/theater").get_json()["clips"]
    assert clips[0]["name"] == "Calm River"
    assert clips[0]["path"] == "Nature/river.mp4"


def test_override_applied_to_playlists(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    (tmp_path / "playlists.json").write_text(
        json.dumps({"playlists": [{"name": "Chill", "clips": [{"path": "Nature/river.mp4", "name": "river"}]}]}),
        encoding="utf-8",
    )
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    pls = c.get("/api/playlists").get_json()["playlists"]
    assert pls[0]["clips"][0]["name"] == "Calm River"


def test_name_control_chars_stripped(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    r = c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "Bad\nName\twith\x00ctrl"})
    assert r.status_code == 200
    name = r.get_json()["name"]
    assert "\n" not in name and "\t" not in name and "\x00" not in name
    assert name == "BadNamewithctrl"


def test_name_length_capped(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    r = c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "x" * 500})
    assert r.status_code == 200
    assert len(r.get_json()["name"]) <= 200


def test_label_may_contain_filename_illegal_chars(tmp_path, monkeypatch):
    # Labels never hit the filesystem, so ':' '/' '?' are allowed (unlike a real filename).
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    r = c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "Trip: Day 1/2?"})
    assert r.status_code == 200
    assert r.get_json()["name"] == "Trip: Day 1/2?"


def test_traversal_path_rejected(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    r = c.post("/api/clip-name", json={"path": "../secret.mp4", "name": "x"})
    assert r.status_code == 404  # unresolvable path → not found, nothing stored


def test_nonexistent_path_rejected(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    r = c.post("/api/clip-name", json={"path": "Nature/ghost.mp4", "name": "x"})
    assert r.status_code == 404


# ── Labels must survive EVERY endpoint that returns clips, not just the GETs.
# Renaming from a tile only writes clip_names.json — theater.json keeps the old
# name — so any response built straight from theater.json reverts the label.
def _seed_theater(tmp_path, clips):
    (tmp_path / "theater.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")


def _two_clips():
    return [{"path": "Nature/river.mp4", "name": "river"},
            {"path": "Nature/forest.mp4", "name": "forest"}]


def test_delete_response_keeps_labels(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _two_clips())
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    clips = c.delete("/api/theater/Nature/forest.mp4").get_json()["clips"]
    assert [x["name"] for x in clips] == ["Calm River"]


def test_add_response_applies_label(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    r = c.post("/api/theater", json={"path": "Nature/river.mp4", "name": "river"})
    assert r.get_json()["clips"][0]["name"] == "Calm River"


def test_reorder_response_applies_labels(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _two_clips())
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    clips = c.post("/api/theater/reorder",
                   json={"paths": ["Nature/forest.mp4", "Nature/river.mp4"]}).get_json()["clips"]
    assert [x["name"] for x in clips] == ["forest", "Calm River"]


def test_loop_response_applies_labels(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _two_clips())
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    clips = c.post("/api/theater/loop",
                   json={"path": "Nature/river.mp4", "loopStart": 5, "loopEnd": 9}).get_json()["clips"]
    assert clips[0]["name"] == "Calm River"


def test_theater_layout_response_applies_labels(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _seed_theater(tmp_path, _two_clips())
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    clips = c.post("/api/theater/layout", json={"layouts": []}).get_json()["clips"]
    assert clips[0]["name"] == "Calm River"


def test_playlist_load_response_applies_labels(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    (tmp_path / "playlists.json").write_text(
        json.dumps({"playlists": [{"name": "Chill", "clips": _two_clips()}]}), encoding="utf-8")
    _seed_clip_names(tmp_path, {"Nature/river.mp4": "Calm River"})
    c = server.app.test_client()
    clips = c.post("/api/playlists/Chill/load").get_json()["clips"]
    assert [x["name"] for x in clips] == ["Calm River", "forest"]


def test_override_persists_across_reload(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    c = server.app.test_client()
    c.post("/api/clip-name", json={"path": "Nature/river.mp4", "name": "Calm River"})
    # Reload the module (simulates a server restart) — override must survive on disk.
    # config.json + media files persist in tmp_path across the reload.
    server2 = make_client(tmp_path, monkeypatch)
    vids = server2.app.test_client().get("/api/videos/Nature").get_json()
    assert vids[0]["name"] == "Calm River"
