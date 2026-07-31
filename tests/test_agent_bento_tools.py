"""Agent: folder creation (with a location prompt) and bento tile sizing."""
import json
import importlib

import ai_agent
from ai_agent import TOOL_NAMES, execute_tool, Sink


def make_client(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(tmp_path))
    import server
    importlib.reload(server)
    server.app.config["TESTING"] = True
    return server


def _root(tmp_path, name="Videos"):
    root = tmp_path / "media"
    root.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text(
        json.dumps({"mediaPaths": [{"path": str(root), "name": name}], "excludedFolders": []}),
        encoding="utf-8")
    return root


def _theater(tmp_path, n):
    clips = [{"path": f"F/c{i}.mp4", "name": f"c{i}"} for i in range(1, n + 1)]
    (tmp_path / "theater.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")
    return {"theaterClips": [dict(c, index=i + 1) for i, c in enumerate(clips)]}


def _saved(tmp_path):
    return json.loads((tmp_path / "theater.json").read_text(encoding="utf-8"))["clips"]


# ── Registry ──
def test_new_tools_are_registered():
    assert {"create_folder", "set_tile_size", "bento_workspace"}.issubset(set(TOOL_NAMES))


def test_bento_workspace_is_a_ui_command():
    # Runs in the browser (it re-arranges live DOM panels), not server-side
    assert "bento_workspace" in ai_agent.UI_COMMAND_TOOLS
    sink = Sink()
    r = execute_tool("bento_workspace", {}, {"workspaceOpen": True}, sink)
    assert {"command": "bento_workspace", "args": {}} in sink.ui_commands
    assert r["status"] == "queued"


def test_prompt_distinguishes_workspace_from_theater_layout():
    from ai_agent import build_system_prompt
    prompt = build_system_prompt({"workspaceOpen": True, "theaterClips": [], "currentVideos": []})
    assert "workspaceOpen" in prompt          # the model can see which surface is in front
    assert "bento_workspace" in prompt


def test_default_model_is_the_agentic_flash():
    assert ai_agent.DEFAULT_MODEL == "gemini-3.6-flash"


# ── create_folder ──
def test_create_folder_without_location_asks_instead_of_guessing(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    root = _root(tmp_path)
    r = execute_tool("create_folder", {"name": "Nature"}, {}, Sink())
    assert r.get("needs_location") is True
    assert any(str(root) == loc["path"] for loc in r["locations"])
    assert not (root / "Nature").exists()      # nothing created while asking


def test_create_folder_creates_dir_and_registers_source(tmp_path, monkeypatch):
    server = make_client(tmp_path, monkeypatch)
    root = _root(tmp_path)
    sink = Sink()
    r = execute_tool("create_folder", {"name": "Nature", "location": str(root)}, {}, sink)
    assert r["status"] == "created"
    assert (root / "Nature").is_dir()
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    added = [s for s in cfg["mediaPaths"] if s["name"] == "Nature"]
    assert added and added[0]["collection"] is True
    assert "folders" in sink.refresh


def test_create_folder_accepts_a_source_name_as_the_location(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    root = _root(tmp_path, name="Videos")
    r = execute_tool("create_folder", {"name": "Nature", "location": "Videos"}, {}, Sink())
    assert r["status"] == "created"
    assert (root / "Nature").is_dir()


def test_create_folder_reports_duplicate(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    root = _root(tmp_path)
    (root / "Nature").mkdir()
    r = execute_tool("create_folder", {"name": "Nature", "location": str(root)}, {}, Sink())
    assert "error" in r


def test_create_folder_without_a_name_asks(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    _root(tmp_path)
    assert "error" in execute_tool("create_folder", {}, {}, Sink())


# ── set_tile_size ──
def test_set_tile_size_writes_bento_cols(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 6)          # 6 clips -> base span 4
    sink = Sink()
    r = execute_tool("set_tile_size", {"sizes": [{"clip": "1", "size": "hero"}]}, ctx, sink)
    assert r["status"] == "ok"
    assert _saved(tmp_path)[0]["bentoCols"] == 12   # 3 x base(4), capped at 12
    assert "theater" in sink.refresh


def test_set_tile_size_composes_a_whole_layout_in_one_call(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 6)
    r = execute_tool("set_tile_size", {"sizes": [
        {"clip": "1", "size": "hero"},
        {"clip": "2", "size": "medium"},
        {"clip": "3", "size": "small"},
    ]}, ctx, Sink())
    saved = _saved(tmp_path)
    assert (saved[0]["bentoCols"], saved[1]["bentoCols"], saved[2]["bentoCols"]) == (12, 8, 4)
    assert r["count"] == 3


def test_set_tile_size_all(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 6)
    execute_tool("set_tile_size", {"sizes": [{"clip": "all", "size": "small"}]}, ctx, Sink())
    assert all(c["bentoCols"] == 4 for c in _saved(tmp_path))


def test_set_tile_size_only_emits_valid_multiples(tmp_path, monkeypatch):
    # Widths must stay whole-tile multiples or the grid strands unfillable gaps (v2.6.1)
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 12)         # 12 clips -> base span 3
    execute_tool("set_tile_size", {"sizes": [
        {"clip": "1", "size": "hero"}, {"clip": "2", "size": "medium"}, {"clip": "3", "size": "small"},
    ]}, ctx, Sink())
    for clip in _saved(tmp_path):
        if "bentoCols" in clip:
            assert clip["bentoCols"] % 3 == 0 and clip["bentoCols"] <= 12


def test_set_tile_size_resolves_clip_by_name(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 6)
    execute_tool("set_tile_size", {"sizes": [{"clip": "c5", "size": "full"}]}, ctx, Sink())
    assert _saved(tmp_path)[4]["bentoCols"] == 12


def test_set_tile_size_unknown_clip_reports_error(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 6)
    r = execute_tool("set_tile_size", {"sizes": [{"clip": "ghost", "size": "hero"}]}, ctx, Sink())
    assert r.get("errors")


def test_set_tile_size_unknown_size_reports_error(tmp_path, monkeypatch):
    make_client(tmp_path, monkeypatch)
    ctx = _theater(tmp_path, 6)
    r = execute_tool("set_tile_size", {"sizes": [{"clip": "1", "size": "gigantic"}]}, ctx, Sink())
    assert r.get("errors")
