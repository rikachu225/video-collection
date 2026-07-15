"""Agent: bulk add-to-theater, label-aware search, and custom theater-name awareness."""
import json
import importlib

from ai_agent import build_system_prompt, execute_tool, Sink


def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server


def _media_root(tmp_path, folder="Nature", filenames=("river.mp4", "forest.mp4")):
    root = tmp_path / "media"
    (root / folder).mkdir(parents=True, exist_ok=True)
    for fn in filenames:
        (root / folder / fn).write_bytes(b"\x00")
    (tmp_path / "config.json").write_text(
        json.dumps({"mediaPaths": [{"path": str(root), "name": "Videos"}], "excludedFolders": []}),
        encoding="utf-8",
    )
    return root


def _ctx_with_videos():
    return {
        "currentFolder": "F",
        "currentVideos": [
            {"index": 1, "name": "A", "path": "F/a.mp4", "folder": "F", "filename": "a.mp4"},
            {"index": 2, "name": "B", "path": "F/b.mp4", "folder": "F", "filename": "b.mp4"},
            {"index": 3, "name": "C", "path": "F/c.mp4", "folder": "F", "filename": "c.mp4"},
        ],
        "theaterClips": [],
    }


def _saved_clips(tmp_path):
    return json.loads((tmp_path / "theater.json").read_text(encoding="utf-8"))["clips"]


# ── Bulk add ("add all of these to my sanctuary") ──
def test_add_to_theater_all_adds_every_video(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    r = execute_tool("add_to_theater", {"video": "all"}, _ctx_with_videos(), Sink())
    assert [c["path"] for c in _saved_clips(tmp_path)] == ["F/a.mp4", "F/b.mp4", "F/c.mp4"]
    assert r["count"] == 3


def test_add_to_theater_everything_synonym(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    execute_tool("add_to_theater", {"video": "everything"}, _ctx_with_videos(), Sink())
    assert len(_saved_clips(tmp_path)) == 3


def test_add_to_theater_single_still_works(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    r = execute_tool("add_to_theater", {"video": "2"}, _ctx_with_videos(), Sink())
    assert [c["path"] for c in _saved_clips(tmp_path)] == ["F/b.mp4"]
    assert r["count"] == 1


def test_add_to_theater_all_skips_already_present(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    execute_tool("add_to_theater", {"video": "all"}, _ctx_with_videos(), Sink())
    r = execute_tool("add_to_theater", {"video": "all"}, _ctx_with_videos(), Sink())  # again
    assert len(_saved_clips(tmp_path)) == 3          # no duplicates
    assert r["count"] == 0
    assert r["skipped"] == 3


def test_add_to_theater_marks_refresh(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    sink = Sink()
    execute_tool("add_to_theater", {"video": "all"}, _ctx_with_videos(), sink)
    assert "theater" in sink.refresh


# ── Label-aware search ──
def test_search_finds_clip_by_its_in_app_label(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    (tmp_path / "clip_names.json").write_text(
        json.dumps({"Nature/river.mp4": "Calm River"}), encoding="utf-8")
    r = execute_tool("search_videos", {"query": "calm"}, {}, Sink())
    assert [m["name"] for m in r["matches"]] == ["Calm River"]


def test_search_still_finds_by_original_filename_after_relabel(tmp_path, monkeypatch):
    # Handy when you've forgotten what a clip was renamed from.
    make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    (tmp_path / "clip_names.json").write_text(
        json.dumps({"Nature/river.mp4": "Calm River"}), encoding="utf-8")
    r = execute_tool("search_videos", {"query": "river"}, {}, Sink())
    # matched via the on-disk stem, but reported under its current label
    assert [m["name"] for m in r["matches"]] == ["Calm River"]


def test_search_unlabelled_clip_reports_stem(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    _media_root(tmp_path)
    r = execute_tool("search_videos", {"query": "forest"}, {}, Sink())
    assert [m["name"] for m in r["matches"]] == ["forest"]


# ── Custom theater name ("Sanctuary") ──
def test_system_prompt_includes_custom_theater_name():
    prompt = build_system_prompt({"theaterName": "Sanctuary", "theaterClips": [], "currentVideos": []})
    assert "Sanctuary" in prompt


def test_system_prompt_theater_name_defaults_gracefully():
    prompt = build_system_prompt({"theaterClips": [], "currentVideos": []})
    assert "Theater" in prompt or "theater" in prompt  # no crash without theaterName
