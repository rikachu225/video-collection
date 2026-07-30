"""Media roots that hold videos DIRECTLY (not in subfolders).

The app was built around "a source root contains subfolders", so a root holding
loose videos was invisible in the sidebar and always counted 0/0 in Settings —
unless it happened to carry the `collection` flag that only create_collection set.
Whether a root is flat is a fact about the filesystem, so it's detected at read
time (which also heals sources already saved without the flag).
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


def _write_config(tmp_path, sources):
    (tmp_path / "config.json").write_text(
        json.dumps({"mediaPaths": sources, "excludedFolders": ["Scripts"]}), encoding="utf-8")


def _mkvideos(directory, names):
    directory.mkdir(parents=True, exist_ok=True)
    for n in names:
        (directory / n).write_bytes(b"\x00")
    return directory


# ── Sidebar (/api/folders) ──
def test_flat_root_appears_as_browseable_folder(tmp_path, monkeypatch):
    flat = _mkvideos(tmp_path / "media" / "Loose Clips", ["a.mp4", "b.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Loose Clips"}])
    server = make_client(tmp_path, monkeypatch)
    tree = server.app.test_client().get("/api/folders").get_json()
    assert [(t["name"], t["path"], t["count"]) for t in tree] == [("Loose Clips", "Loose Clips", 2)]


def test_mixed_root_exposes_root_and_subfolders(tmp_path, monkeypatch):
    root = _mkvideos(tmp_path / "media" / "Mixed", ["loose.mp4"])
    _mkvideos(root / "Sub", ["s1.mp4", "s2.mp4"])
    _write_config(tmp_path, [{"path": str(root), "name": "Mixed"}])
    server = make_client(tmp_path, monkeypatch)
    tree = server.app.test_client().get("/api/folders").get_json()
    assert [(t["path"], t["count"]) for t in tree] == [("Mixed", 1), ("Sub", 2)]


def test_subfolder_only_root_unchanged(tmp_path, monkeypatch):
    # Regression: the existing "root of subfolders" model must not gain a root entry.
    root = tmp_path / "media" / "Cts"
    _mkvideos(root / "A", ["a.mp4"])
    _write_config(tmp_path, [{"path": str(root), "name": "Cts"}])
    server = make_client(tmp_path, monkeypatch)
    tree = server.app.test_client().get("/api/folders").get_json()
    assert [t["path"] for t in tree] == ["A"]


# ── Settings counts (/api/sources) ──
def test_source_counts_include_direct_videos(tmp_path, monkeypatch):
    flat = _mkvideos(tmp_path / "media" / "Flat", ["a.mp4", "b.mp4", "c.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Flat"}])
    server = make_client(tmp_path, monkeypatch)
    s = server.app.test_client().get("/api/sources").get_json()["sources"][0]
    assert (s["folders"], s["videos"]) == (1, 3)


def test_collection_source_is_not_counted_as_zero(tmp_path, monkeypatch):
    # A collection created in-app still reported 0 folders / 0 videos.
    flat = _mkvideos(tmp_path / "media" / "Archive", ["a.mp4", "b.mp4", "c.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Archive", "collection": True}])
    server = make_client(tmp_path, monkeypatch)
    s = server.app.test_client().get("/api/sources").get_json()["sources"][0]
    assert (s["folders"], s["videos"]) == (1, 3)


def test_source_counts_match_sidebar_entries(tmp_path, monkeypatch):
    # Drift guard: Settings counts and the sidebar must come from the same logic.
    root = _mkvideos(tmp_path / "media" / "Mixed", ["loose.mp4"])
    _mkvideos(root / "Sub", ["s1.mp4", "s2.mp4"])
    _write_config(tmp_path, [{"path": str(root), "name": "Mixed"}])
    server = make_client(tmp_path, monkeypatch)
    c = server.app.test_client()
    tree = c.get("/api/folders").get_json()
    s = c.get("/api/sources").get_json()["sources"][0]
    assert s["folders"] == len(tree)
    assert s["videos"] == sum(t["count"] for t in tree)


# ── Opening a flat root (/api/videos) ──
def test_get_videos_from_flat_root(tmp_path, monkeypatch):
    flat = _mkvideos(tmp_path / "media" / "Flat", ["a.mp4", "b.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Flat"}])
    server = make_client(tmp_path, monkeypatch)
    vids = server.app.test_client().get("/api/videos/Flat?source=0").get_json()
    assert [v["path"] for v in vids] == ["Flat/a.mp4", "Flat/b.mp4"]


def test_get_videos_flat_root_without_source_index(tmp_path, monkeypatch):
    # The AI assistant's list_videos calls get_videos() with no source index.
    flat = _mkvideos(tmp_path / "media" / "Flat", ["a.mp4", "b.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Flat"}])
    server = make_client(tmp_path, monkeypatch)
    assert len(server.app.test_client().get("/api/videos/Flat").get_json()) == 2


# ── Streaming / thumbnails resolve for flat roots ──
def test_resolve_video_path_flat_root(tmp_path, monkeypatch):
    flat = _mkvideos(tmp_path / "media" / "Flat", ["a.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Flat"}])
    server = make_client(tmp_path, monkeypatch)
    assert server._resolve_video_path("Flat/a.mp4") == flat / "a.mp4"


def test_flat_root_does_not_resolve_foreign_folder_segment(tmp_path, monkeypatch):
    # Hardening: the leading segment must name the root. Previously ANY first
    # segment was stripped for collection sources, so an unrelated folder name
    # could resolve to a same-named file in the collection root.
    flat = _mkvideos(tmp_path / "media" / "Archive", ["clip.mp4"])
    _write_config(tmp_path, [{"path": str(flat), "name": "Archive", "collection": True}])
    server = make_client(tmp_path, monkeypatch)
    assert server._resolve_video_path("SomeOtherFolder/clip.mp4") is None
