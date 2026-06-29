"""AI assistant: maps natural-language chat to allow-listed app actions via Gemini.

No Flask imports here. Server-action executors lazily `import server` so this module
stays import-safe and unit-testable without starting the web server.
"""

import logging
import os
import re

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# google-genai logs a WARNING about non-text 'thought_signature' parts whenever response.text
# is accessed on a thinking-model response. It's harmless here (we pass the full model content
# back through the tool loop), so quiet that logger — real errors still surface.
logging.getLogger("google_genai.types").setLevel(logging.ERROR)

# "gemini-flash-latest" is an alias that auto-tracks Google's newest flash model, so the
# assistant doesn't break when an older model is retired. Override per-install via aiAssistant.model.
DEFAULT_MODEL = "gemini-flash-latest"
MAX_ITERATIONS = 5


# ── Pure helpers ──────────────────────────────────────────────
def parse_time_to_seconds(value):
    """Parse '1:20', '1:02:03', '80', '1m20s', '90s', '2m', int/float -> seconds or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if value >= 0 else None
    s = str(value).strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", s)
    if m and (m.group(1) or m.group(2)):
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if any(n < 0 for n in nums):
            return None
        if len(parts) == 2:
            return nums[0] * 60 + nums[1]
        if len(parts) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if n >= 0 else None


_ALL_WORDS = {"all", "everything", "every clip", "every", "*", "them all"}


def resolve_refs(ref, clips):
    """Resolve a clip/video reference to a list of (index, item) tuples.

    ref may be: a 1-based int index, a digit string, 'all'/'everything', or a
    case-insensitive name substring. Returns [] if nothing matches.
    """
    if not clips:
        return []
    if isinstance(ref, str) and ref.strip().lower() in _ALL_WORDS:
        return list(enumerate(clips))
    if isinstance(ref, bool):
        return []
    if isinstance(ref, (int, float)) or (isinstance(ref, str) and ref.strip().isdigit()):
        i = int(ref) - 1
        return [(i, clips[i])] if 0 <= i < len(clips) else []
    needle = str(ref).strip().lower()
    if not needle:
        return []
    return [(i, c) for i, c in enumerate(clips) if needle in c.get("name", "").lower()]


# ── Config / key resolution ───────────────────────────────────
def is_available():
    """True if the google-genai SDK is importable."""
    return GENAI_AVAILABLE


def get_api_key(config):
    """Resolve the Gemini key: GEMINI_API_KEY env var first, then config['geminiApiKey']."""
    env = os.environ.get("GEMINI_API_KEY", "").strip()
    if env:
        return env
    return (config.get("geminiApiKey") or "").strip()


def get_model(config):
    ai = config.get("aiAssistant") or {}
    return ai.get("model") or DEFAULT_MODEL


def assistant_enabled(config):
    ai = config.get("aiAssistant") or {}
    return ai.get("enabled", True)


# ── Tool declarations ─────────────────────────────────────────
def _str(desc):
    return {"type": "string", "description": desc}


# name -> (description, json-schema parameters)
_TOOL_DEFS = {
    "set_loop": ("Set a loop on a theater clip.", {
        "type": "object",
        "properties": {
            "clip": _str("Clip reference: 1-based index, a name substring, or 'all'."),
            "start": _str("Loop start, e.g. '1:20', '80', '1m20s'."),
            "end": _str("Loop end, same formats as start."),
        }, "required": ["clip", "start", "end"]}),
    "clear_loop": ("Remove the loop from a theater clip.", {
        "type": "object",
        "properties": {"clip": _str("Clip reference: index, name, or 'all'.")},
        "required": ["clip"]}),
    "save_playlist": ("Save the current theater as a named playlist.", {
        "type": "object", "properties": {"name": _str("Playlist name.")}, "required": ["name"]}),
    "load_playlist": ("Load a saved playlist into the theater.", {
        "type": "object", "properties": {"name": _str("Playlist name (fuzzy match).")},
        "required": ["name"]}),
    "list_playlists": ("List all saved playlist names.", {"type": "object", "properties": {}}),
    "rename_playlist": ("Rename a playlist.", {
        "type": "object",
        "properties": {"old": _str("Existing name."), "new": _str("New name.")},
        "required": ["old", "new"]}),
    "add_to_theater": ("Add a video from the current folder to the theater.", {
        "type": "object",
        "properties": {"video": _str("Video reference: 1-based index or name substring in the current folder.")},
        "required": ["video"]}),
    "remove_from_theater": ("Remove a clip from the theater (does NOT delete the file).", {
        "type": "object", "properties": {"clip": _str("Clip reference: index, name, or 'all'.")},
        "required": ["clip"]}),
    "download": ("Download a video from a URL into a folder via yt-dlp.", {
        "type": "object",
        "properties": {"url": _str("Video URL."), "folder": _str("Destination folder name.")},
        "required": ["url", "folder"]}),
    "download_to_theater": ("Download a URL and add it straight to the theater.", {
        "type": "object", "properties": {"url": _str("Video URL.")}, "required": ["url"]}),
    "search_videos": ("Search all folders for videos by name.", {
        "type": "object", "properties": {"query": _str("Search text.")}, "required": ["query"]}),
    "list_folders": ("List all library folders with video counts.", {"type": "object", "properties": {}}),
    "list_videos": ("List videos in a folder.", {
        "type": "object", "properties": {"folder": _str("Folder name.")}, "required": ["folder"]}),
    "get_stats": ("Get library stats (sources, folders, video counts).", {"type": "object", "properties": {}}),
    "play_all": ("Play all videos in the current view/theater.", {"type": "object", "properties": {}}),
    "pause_all": ("Pause all videos.", {"type": "object", "properties": {}}),
    "mute_all": ("Mute all videos.", {"type": "object", "properties": {}}),
    "unmute_all": ("Unmute all videos.", {"type": "object", "properties": {}}),
    "open_workspace": ("Open fullscreen workspace mode.", {"type": "object", "properties": {}}),
    "close_workspace": ("Close workspace mode.", {"type": "object", "properties": {}}),
    "switch_view": ("Switch the main view.", {
        "type": "object",
        "properties": {"view": {"type": "string", "enum": ["browse", "theater", "playlists"]}},
        "required": ["view"]}),
    "open_folder": ("Open a folder in the browse view.", {
        "type": "object", "properties": {"folder": _str("Folder name.")}, "required": ["folder"]}),
}

TOOL_NAMES = list(_TOOL_DEFS.keys())
# UI-command tools are executed by the BROWSER, not server-side.
UI_COMMAND_TOOLS = {
    "play_all", "pause_all", "mute_all", "unmute_all",
    "open_workspace", "close_workspace", "switch_view", "open_folder",
}


def build_tool():
    """Build the genai Tool from declarations. Requires the SDK."""
    decls = [
        types.FunctionDeclaration(name=n, description=d, parameters_json_schema=p)
        for n, (d, p) in _TOOL_DEFS.items()
    ]
    return types.Tool(function_declarations=decls)


def build_system_prompt(ctx):
    """Build the system instruction with the live context snapshot."""
    import json as _json
    return (
        "You are the in-app assistant for a personal video collection app. "
        "Translate the user's request into the provided tools. Only act on what the user asks. "
        "Use the CONTEXT below to resolve references like 'the third clip' or 'that video' to a "
        "clip index or name. If a reference is ambiguous (matches several items) or matches none, "
        "ask a brief clarifying question instead of guessing. "
        "If a video is currently open (see openVideo in the context), interpret 'this', "
        "'this clip', 'the current one', or 'it' as that open clip — use its theaterIndex. "
        "If a playlist is currently loaded (see loadedPlaylist), 'save this playlist' or "
        "'update this playlist' means overwrite that loaded playlist — pass its name to save_playlist. "
        "Times use m:ss (e.g. 1:20). "
        "IMPORTANT: to perform ANY action you MUST call the matching tool in this turn. "
        "Never claim you did something (e.g. 'Done', 'Opened', 'Saved') unless you actually "
        "called its tool — if you can't do it, briefly say why instead. "
        "Keep replies short and friendly. After acting, confirm what you did in one sentence.\n\n"
        "CONTEXT (current app state):\n" + _json.dumps({
            "currentView": ctx.get("currentView"),
            "currentFolder": ctx.get("currentFolder"),
            "theaterClips": ctx.get("theaterClips", []),
            "currentFolderVideos": ctx.get("currentVideos", []),
            "playlists": ctx.get("playlistNames", []),
            "loadedPlaylist": ctx.get("loadedPlaylist"),
            "openVideo": ctx.get("openVideo"),
        }, ensure_ascii=False, indent=2)
    )


# ── Side-effect sink + server-side wrappers ───────────────────
class Sink:
    """Collects side-effects produced while executing tools in one turn."""
    def __init__(self):
        self.ui_commands = []
        self.refresh = set()

    def ui(self, command, args=None):
        self.ui_commands.append({"command": command, "args": args or {}})

    def mark(self, *views):
        self.refresh.update(views)


def _srv_set_loop(path, start, end):
    import server
    data = server._load_json(server.THEATER_FILE, {"clips": []})
    for clip in data["clips"]:
        if clip["path"] == path:
            clip["loopStart"] = start
            clip["loopEnd"] = end
    server._save_json(server.THEATER_FILE, data)


def _srv_save_playlist(name, clips):
    import server
    data = server._load_json(server.PLAYLISTS_FILE, {"playlists": []})
    for pl in data["playlists"]:
        if pl["name"] == name:
            pl["clips"] = clips
            server._save_json(server.PLAYLISTS_FILE, data)
            return
    data["playlists"].append({"name": name, "clips": clips})
    server._save_json(server.PLAYLISTS_FILE, data)


def _srv_load_playlist(name):
    """Load a playlist into the theater. Returns the matched playlist name, or None."""
    import server
    data = server._load_json(server.PLAYLISTS_FILE, {"playlists": []})
    for pl in data["playlists"]:
        if pl["name"].lower() == name.lower():
            server._save_json(server.THEATER_FILE, {"clips": pl["clips"]})
            return pl["name"]
    for pl in data["playlists"]:
        if name.lower() in pl["name"].lower():
            server._save_json(server.THEATER_FILE, {"clips": pl["clips"]})
            return pl["name"]
    return None


def _srv_playlist_names():
    import server
    data = server._load_json(server.PLAYLISTS_FILE, {"playlists": []})
    return [p["name"] for p in data["playlists"]]


def _srv_delete_playlist(name):
    import server
    data = server._load_json(server.PLAYLISTS_FILE, {"playlists": []})
    data["playlists"] = [p for p in data["playlists"] if p["name"] != name]
    server._save_json(server.PLAYLISTS_FILE, data)


def _srv_remove_from_theater(path):
    import server
    data = server._load_json(server.THEATER_FILE, {"clips": []})
    data["clips"] = [c for c in data["clips"] if c["path"] != path]
    server._save_json(server.THEATER_FILE, data)


def _srv_add_to_theater(clip):
    import server
    data = server._load_json(server.THEATER_FILE, {"clips": []})
    if clip["path"] not in {c["path"] for c in data["clips"]}:
        data["clips"].append(clip)
        server._save_json(server.THEATER_FILE, data)


def execute_tool(name, args, ctx, sink):
    """Execute one tool call. Returns a JSON-able result dict for the model."""
    args = args or {}

    # Workspace open is context-aware: open the theater if it has clips, else the
    # currently-open folder's videos, else report nothing to show (so the model
    # gives an honest answer instead of a false "Done").
    if name == "open_workspace":
        if ctx.get("theaterClips"):
            sink.ui("open_workspace", {"source": "theater"})
            return {"status": "ok", "source": "theater"}
        if ctx.get("currentVideos"):
            sink.ui("open_workspace", {"source": "browse"})
            return {"status": "ok", "source": "browse", "folder": ctx.get("currentFolder")}
        return {"error": "Nothing to open — the Theater is empty and no folder is open. "
                         "Add clips to the Theater or open a folder first."}

    # UI commands: queue for the browser, don't run server-side.
    if name in UI_COMMAND_TOOLS:
        sink.ui(name, args)
        return {"status": "queued"}

    if name == "set_loop":
        matches = resolve_refs(args.get("clip"), ctx.get("theaterClips", []))
        if not matches:
            return {"error": f"No theater clip matches '{args.get('clip')}'."}
        start = parse_time_to_seconds(args.get("start"))
        end = parse_time_to_seconds(args.get("end"))
        if start is None or end is None or end <= start:
            return {"error": "Invalid loop times; end must be after start (use m:ss)."}
        for _, clip in matches:
            _srv_set_loop(clip["path"], start, end)
        sink.mark("theater")
        return {"status": "ok", "looped": [c["name"] for _, c in matches], "start": start, "end": end}

    if name == "clear_loop":
        matches = resolve_refs(args.get("clip"), ctx.get("theaterClips", []))
        if not matches:
            return {"error": f"No theater clip matches '{args.get('clip')}'."}
        for _, clip in matches:
            _srv_set_loop(clip["path"], None, None)
        sink.mark("theater")
        return {"status": "ok", "cleared": [c["name"] for _, c in matches]}

    if name == "save_playlist":
        pname = (args.get("name") or "").strip()
        # "save this playlist" with no real name → use the currently-loaded playlist.
        if pname.lower() in ("", "this", "this playlist", "the current playlist",
                             "current playlist", "it"):
            pname = (ctx.get("loadedPlaylist") or "").strip()
        if not pname:
            return {"error": "No playlist is loaded — what would you like to name this playlist?"}
        _srv_save_playlist(pname, ctx.get("theaterClips", []))
        sink.mark("playlists")
        sink.ui("set_loaded_playlist", {"name": pname})
        return {"status": "ok", "saved": pname}

    if name == "load_playlist":
        loaded = _srv_load_playlist(args.get("name", ""))
        if not loaded:
            return {"error": f"No playlist matches '{args.get('name')}'. Known: {_srv_playlist_names()}"}
        sink.mark("theater")
        sink.ui("switch_view", {"view": "theater"})
        sink.ui("set_loaded_playlist", {"name": loaded})
        return {"status": "ok", "loaded": loaded}

    if name == "list_playlists":
        return {"playlists": _srv_playlist_names()}

    if name == "rename_playlist":
        names = _srv_playlist_names()
        old = args.get("old", "")
        match = next((n for n in names if n.lower() == old.lower()), None)
        if not match:
            return {"error": f"No playlist named '{old}'. Known: {names}"}
        import server
        data = server._load_json(server.PLAYLISTS_FILE, {"playlists": []})
        clips = next(p["clips"] for p in data["playlists"] if p["name"] == match)
        _srv_save_playlist(args.get("new", match), clips)
        _srv_delete_playlist(match)
        sink.mark("playlists")
        return {"status": "ok", "renamed": [match, args.get("new")]}

    if name == "add_to_theater":
        matches = resolve_refs(args.get("video"), ctx.get("currentVideos", []))
        if not matches:
            return {"error": f"No video in the current folder matches '{args.get('video')}'."}
        _, v = matches[0]
        clip = {"path": v["path"], "name": v["name"], "filename": v.get("filename"),
                "folder": v.get("folder"), "loopStart": None, "loopEnd": None}
        _srv_add_to_theater(clip)
        sink.mark("theater")
        return {"status": "ok", "added": v["name"]}

    if name == "remove_from_theater":
        matches = resolve_refs(args.get("clip"), ctx.get("theaterClips", []))
        if not matches:
            return {"error": f"No theater clip matches '{args.get('clip')}'."}
        for _, clip in matches:
            _srv_remove_from_theater(clip["path"])
        sink.mark("theater")
        return {"status": "ok", "removed": [c["name"] for _, c in matches]}

    if name in ("download", "download_to_theater"):
        return _execute_download(name, args, ctx, sink)

    if name == "search_videos":
        return _search_videos(args.get("query", ""))

    if name == "list_folders":
        import server
        return {"folders": [{"name": f["name"], "count": f["count"]} for f in server.get_folders().json]}

    if name == "list_videos":
        import server
        with server.app.test_request_context():
            resp = server.get_videos(args.get("folder", ""))
        data = resp.json if hasattr(resp, "json") else resp[0].json
        return {"videos": [{"name": v["name"]} for v in data]} if isinstance(data, list) else {"error": "Folder not found"}

    if name == "get_stats":
        import server
        sources = server._get_media_roots()
        folders = server.get_folders().json
        return {"sources": len(sources), "folders": len(folders),
                "videos": sum(f["count"] for f in folders)}

    return {"error": f"Unknown tool '{name}'."}


def _execute_download(name, args, ctx, sink):
    import server
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "A URL is required."}
    if not server.YT_DLP_AVAILABLE:
        return {"error": "yt-dlp is not installed on the server."}
    if name == "download_to_theater":
        with server.app.test_request_context(json={"url": url}):
            resp = server.cache_external()
        body = resp.json if hasattr(resp, "json") else resp[0].json
        if body.get("error"):
            return {"error": body["error"]}
        _srv_add_to_theater(body["clip"])
        sink.mark("theater")
        return {"status": "ok", "downloaded": body["clip"]["name"]}
    folder = (args.get("folder") or ctx.get("currentFolder") or "").strip()
    payload = {"url": url, "folder": folder, "sourceIndex": ctx.get("currentSourceIndex")}
    with server.app.test_request_context(json=payload):
        resp = server.folder_download()
    body = resp.json if hasattr(resp, "json") else resp[0].json
    if body.get("error"):
        return {"error": body["error"]}
    sink.mark("folders")
    return {"status": "ok", "downloaded": body["video"]["name"], "folder": folder}


def _search_videos(query):
    import server
    q = query.strip().lower()
    if not q:
        return {"matches": []}
    matches = []
    for source in server._get_media_roots():
        root = server.Path(source["path"])
        if not root.exists():
            continue
        for item in root.rglob("*"):
            if item.is_file() and item.suffix.lower() in server.VIDEO_EXTENSIONS:
                if q in item.stem.lower():
                    matches.append({"name": item.stem, "folder": item.parent.name})
                    if len(matches) >= 25:
                        return {"matches": matches, "truncated": True}
    return {"matches": matches}


# ── Agent loop ────────────────────────────────────────────────
def _to_contents(history, message):
    """Build the genai contents list from prior history + the new user message."""
    contents = []
    for turn in history[-10:]:
        role = "model" if turn.get("role") == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=turn.get("text", ""))]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
    return contents


def run_agent(message, history, ctx, config, client=None):
    """Run the function-calling loop. Returns {reply, ui_commands, refresh}.

    `client` is injectable for tests; in production we build one from the key.
    """
    sink = Sink()
    if client is None:
        if not GENAI_AVAILABLE:
            return {"reply": "The AI SDK isn't installed on the server.", "ui_commands": [], "refresh": []}
        client = genai.Client(api_key=get_api_key(config))

    model = get_model(config)
    tool = build_tool()
    gen_config = types.GenerateContentConfig(
        tools=[tool],
        system_instruction=build_system_prompt(ctx),
    )
    contents = _to_contents(history, message)

    last_text = ""
    for _ in range(MAX_ITERATIONS):
        response = client.models.generate_content(model=model, contents=contents, config=gen_config)
        fcs = list(response.function_calls or [])
        if not fcs:
            last_text = response.text or last_text
            break
        contents.append(response.candidates[0].content)
        tool_parts = []
        for fc in fcs:
            result = execute_tool(fc.name, dict(fc.args or {}), ctx, sink)
            tool_parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="tool", parts=tool_parts))
    else:
        last_text = last_text or "I did as much as I could in one go — ask me to continue if needed."

    return {"reply": last_text or "Done.", "ui_commands": sink.ui_commands, "refresh": sorted(sink.refresh)}
