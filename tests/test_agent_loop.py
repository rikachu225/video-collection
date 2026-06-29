from ai_agent import (
    TOOL_NAMES, build_system_prompt, execute_tool, Sink, run_agent,
)


# ── Task 5: schema + system prompt ──
def test_tool_names_cover_selected_capabilities():
    expected = {
        "set_loop", "clear_loop", "save_playlist", "load_playlist", "list_playlists",
        "rename_playlist", "add_to_theater", "remove_from_theater", "download",
        "download_to_theater", "search_videos", "list_folders", "list_videos",
        "get_stats", "play_all", "pause_all", "mute_all", "unmute_all",
        "open_workspace", "close_workspace", "switch_view", "open_folder",
    }
    assert expected.issubset(set(TOOL_NAMES))


def test_system_prompt_includes_context_snapshot():
    ctx = {
        "currentView": "theater",
        "currentFolder": "Fallen",
        "theaterClips": [{"index": 1, "name": "Pink Sunset", "loopStart": None, "loopEnd": None}],
        "currentVideos": [{"index": 1, "name": "Clip A"}],
        "playlistNames": ["chill"],
    }
    prompt = build_system_prompt(ctx)
    assert "Pink Sunset" in prompt
    assert "theater" in prompt
    assert "chill" in prompt


def test_system_prompt_surfaces_open_video():
    ctx = {
        "currentView": "theater",
        "theaterClips": [{"index": 1, "name": "Naomi"}],
        "openVideo": {"name": "Naomi", "path": "x/n.mp4", "theaterIndex": 1},
    }
    prompt = build_system_prompt(ctx)
    assert "openVideo" in prompt          # the open clip rides along in context
    assert "the current one" in prompt    # the 'this'/'current' instruction is present


# ── Task 6: executors ──
def make_ctx():
    return {
        "currentView": "theater",
        "currentFolder": "Fallen",
        "currentSourceIndex": 0,
        "theaterClips": [
            {"index": 1, "name": "Pink Sunset", "path": "Fallen/a.mp4", "loopStart": None, "loopEnd": None},
            {"index": 2, "name": "Blue Dawn", "path": "Fallen/b.mp4", "loopStart": None, "loopEnd": None},
        ],
        "currentVideos": [],
        "playlistNames": [],
    }


def test_ui_command_tool_is_queued_not_executed():
    sink = Sink()
    result = execute_tool("play_all", {}, make_ctx(), sink)
    assert {"command": "play_all", "args": {}} in sink.ui_commands
    assert result["status"] == "queued"


def test_switch_view_queues_with_args():
    sink = Sink()
    execute_tool("switch_view", {"view": "playlists"}, make_ctx(), sink)
    assert {"command": "switch_view", "args": {"view": "playlists"}} in sink.ui_commands


def test_set_loop_resolves_and_calls_server(monkeypatch):
    calls = []
    import ai_agent
    monkeypatch.setattr(ai_agent, "_srv_set_loop", lambda path, start, end: calls.append((path, start, end)))
    sink = Sink()
    result = execute_tool("set_loop", {"clip": "2", "start": "1:00", "end": "1:30"}, make_ctx(), sink)
    assert calls == [("Fallen/b.mp4", 60, 90)]
    assert "theater" in sink.refresh
    assert result["status"] == "ok"


def test_set_loop_bad_reference_returns_error():
    sink = Sink()
    result = execute_tool("set_loop", {"clip": "nope", "start": "1:00", "end": "1:30"}, make_ctx(), sink)
    assert "error" in result


def test_open_workspace_empty_returns_error_no_command():
    sink = Sink()
    ctx = {"theaterClips": [], "currentVideos": [], "currentFolder": None}
    r = execute_tool("open_workspace", {}, ctx, sink)
    assert "error" in r
    assert sink.ui_commands == []


def test_open_workspace_uses_theater_when_clips_present():
    sink = Sink()
    r = execute_tool("open_workspace", {}, make_ctx(), sink)
    assert r.get("status") == "ok"
    assert {"command": "open_workspace", "args": {"source": "theater"}} in sink.ui_commands


def test_open_workspace_falls_back_to_browse_folder():
    sink = Sink()
    ctx = {"theaterClips": [], "currentVideos": [{"index": 1, "name": "v", "path": "F/v.mp4"}], "currentFolder": "F"}
    r = execute_tool("open_workspace", {}, ctx, sink)
    assert r.get("status") == "ok"
    assert {"command": "open_workspace", "args": {"source": "browse"}} in sink.ui_commands


def test_save_playlist_explicit_name(monkeypatch):
    saved = []
    import ai_agent
    monkeypatch.setattr(ai_agent, "_srv_save_playlist", lambda n, c: saved.append(n))
    sink = Sink()
    r = execute_tool("save_playlist", {"name": "chill mix"}, make_ctx(), sink)
    assert saved == ["chill mix"]
    assert r["status"] == "ok"


def test_save_playlist_this_uses_loaded_playlist(monkeypatch):
    saved = []
    import ai_agent
    monkeypatch.setattr(ai_agent, "_srv_save_playlist", lambda n, c: saved.append(n))
    ctx = dict(make_ctx(), loadedPlaylist="Night Mode")
    sink = Sink()
    r = execute_tool("save_playlist", {"name": "this playlist"}, ctx, sink)
    assert saved == ["Night Mode"]
    assert r["status"] == "ok"
    assert {"command": "set_loaded_playlist", "args": {"name": "Night Mode"}} in sink.ui_commands


def test_save_playlist_this_without_loaded_asks_for_name(monkeypatch):
    import ai_agent
    monkeypatch.setattr(ai_agent, "_srv_save_playlist", lambda n, c: None)
    sink = Sink()
    r = execute_tool("save_playlist", {"name": "this"}, make_ctx(), sink)
    assert "error" in r


def test_load_playlist_marks_loaded(monkeypatch):
    import ai_agent
    monkeypatch.setattr(ai_agent, "_srv_load_playlist", lambda n: "Chill Mix")
    sink = Sink()
    r = execute_tool("load_playlist", {"name": "chill"}, make_ctx(), sink)
    assert r["status"] == "ok"
    assert {"command": "set_loaded_playlist", "args": {"name": "Chill Mix"}} in sink.ui_commands


def test_system_prompt_surfaces_loaded_playlist():
    ctx = {"theaterClips": [{"index": 1, "name": "x"}], "loadedPlaylist": "Night Mode"}
    p = build_system_prompt(ctx)
    assert "loadedPlaylist" in p
    assert "this playlist" in p.lower()


# ── Task 7: agent loop with a fake client ──
class _FakeFC:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakeContent:
    pass


class _FakeResponse:
    def __init__(self, function_calls=None, text=None):
        self.function_calls = function_calls or []
        self.text = text
        self.candidates = [type("C", (), {"content": _FakeContent()})()]


class _FakeModels:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = 0

    def generate_content(self, **kwargs):
        resp = self._scripted[self.calls]
        self.calls += 1
        return resp


class _FakeClient:
    def __init__(self, scripted):
        self.models = _FakeModels(scripted)


def test_run_agent_executes_tool_then_returns_reply(monkeypatch):
    captured = []
    import ai_agent
    monkeypatch.setattr(ai_agent, "_srv_set_loop", lambda p, s, e: captured.append((p, s, e)))
    scripted = [
        _FakeResponse(function_calls=[_FakeFC("set_loop", {"clip": "1", "start": "0:10", "end": "0:20"})]),
        _FakeResponse(text="Done — looped Pink Sunset from 0:10 to 0:20."),
    ]
    out = run_agent("loop the first clip 0:10 to 0:20", [], make_ctx(), {}, client=_FakeClient(scripted))
    assert captured == [("Fallen/a.mp4", 10, 20)]
    assert "Done" in out["reply"]
    assert "theater" in out["refresh"]


def test_run_agent_returns_ui_commands():
    scripted = [
        _FakeResponse(function_calls=[_FakeFC("play_all", {})]),
        _FakeResponse(text="Playing everything."),
    ]
    out = run_agent("play all", [], make_ctx(), {}, client=_FakeClient(scripted))
    assert {"command": "play_all", "args": {}} in out["ui_commands"]


def test_run_agent_plain_reply_no_tools():
    scripted = [_FakeResponse(text="You have 2 clips loaded.")]
    out = run_agent("how many clips?", [], make_ctx(), {}, client=_FakeClient(scripted))
    assert out["reply"] == "You have 2 clips loaded."
    assert out["ui_commands"] == []
