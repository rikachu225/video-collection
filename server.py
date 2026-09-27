"""
Video Collection - Backend Server
Serves video files and manages playlists/theater state.
Uses waitress for production-grade serving on Windows.
"""

import hashlib
import json
import os
import re
import shutil
import mimetypes
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from urllib.parse import urlparse, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler
from urllib.error import HTTPError, URLError
import socket
import ssl
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import lru_cache, partial
from flask import Flask, jsonify, request, send_file, Response, send_from_directory

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

# No CORS: the SPA is served from this same origin and never needs it. A blanket
# CORS(app) let any website the user visited read and write this API.
app = Flask(__name__, static_folder="static", static_url_path="")


# ── LAN-only guard ─────────────────────────────────────────────
# Allows localhost + private RFC-1918 ranges only.
# Blocks any request from a public IP (just in case the machine
# is ever on a network without NAT, or port forwarding is set up).
import ipaddress

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),    # loopback
    ipaddress.ip_network("10.0.0.0/8"),      # private class A
    ipaddress.ip_network("172.16.0.0/12"),   # private class B
    ipaddress.ip_network("192.168.0.0/16"),  # private class C
    ipaddress.ip_network("::1/128"),         # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),        # IPv6 unique local
]

@app.before_request
def _lan_only():
    try:
        ip = ipaddress.ip_address(request.remote_addr)
        if not any(ip in net for net in _PRIVATE_NETWORKS):
            return jsonify({"error": "Access restricted to local network"}), 403
    except ValueError:
        return jsonify({"error": "Invalid remote address"}), 403


# ── Same-origin guard ──────────────────────────────────────────
# The IP check above can't tell the user's browser apart from a web page it is
# visiting: both arrive from 127.0.0.1 or the LAN. Two browser-driven attacks remain:
#  - DNS rebinding: evil.example re-resolves to 127.0.0.1 and then talks to this API
#    as its own origin. The browser still sends `Host: evil.example`, so only this
#    machine's names and local IP literals are accepted as Host.
#  - Cross-site writes: any page can POST/DELETE here without reading the reply.
#    Browsers send Origin (and Sec-Fetch-Site) on those, so a state-changing request
#    must come from this app's own origin.
#  - Cross-site reads: <img>/<video>/<script> on any page make the browser GET /api/*
#    too. The body stays unreadable without CORS, but onload vs onerror leaks the
#    status (which custom services exist, which library paths do), and a GET can start
#    work here (an icon lookup). Browsers label each request with Sec-Fetch-Site, so
#    /api/* refuses "cross-site" and "same-site" (another port of this machine is
#    same-site). "same-origin" (the SPA itself) and "none" (typed URL, bookmark) pass.
# Clients that send no Host / Origin / Sec-Fetch-Site header (curl, scripts, monitors)
# are not browsers and are left to the IP guard. Browsers send fetch metadata only to
# https and localhost origins, so on a plain-http LAN address the read check is inert.
_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_FOREIGN_FETCH_SITES = {"cross-site", "same-site"}
_HOST_HEADER_RE = re.compile(r"^(?:\[([0-9A-Fa-f:.]+)\]|([A-Za-z0-9._-]+))(?::\d{1,5})?$")


@lru_cache(maxsize=1)
def _own_hostnames():
    """Names this machine answers to. Extra names (a router DNS alias, a hosts-file
    entry) can be allowed with VIDCOL_ALLOWED_HOSTS=media.lan,nas.home."""
    names = {"localhost"}
    try:
        host = socket.gethostname().strip().rstrip(".").lower()
        if host:
            names.update({host, host + ".local"})
        fqdn = socket.getfqdn().strip().rstrip(".").lower()
        if fqdn:
            names.add(fqdn)
    except OSError:
        pass
    extra = os.environ.get("VIDCOL_ALLOWED_HOSTS", "")
    names.update(h.strip().rstrip(".").lower() for h in extra.split(",") if h.strip())
    return frozenset(names)


def _host_allowed(host):
    """localhost, a loopback/private/link-local IP literal, or one of our own names."""
    m = _HOST_HEADER_RE.match(host)
    if not m:
        return False
    name = (m.group(1) or m.group(2)).rstrip(".").lower()
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        # A bracketed Host must be an IPv6 literal; anything else is a name.
        return m.group(1) is None and (name == "localhost" or name in _own_hostnames())
    return ip.is_loopback or ip.is_private or ip.is_link_local


@app.before_request
def _same_origin_only():
    host = request.environ.get("HTTP_HOST", "")
    if host and not _host_allowed(host):
        return jsonify({"error": "Unrecognised Host header"}), 403
    fetch_site = request.headers.get("Sec-Fetch-Site", "").strip().lower()
    if fetch_site in _FOREIGN_FETCH_SITES and (request.path == "/api"
                                               or request.path.startswith("/api/")):
        return jsonify({"error": "Cross-site request refused"}), 403
    if request.method in _STATE_CHANGING_METHODS:
        origin = request.headers.get("Origin")
        own = f"{request.scheme}://{host or request.host}".lower()
        if origin is not None and origin.strip().lower() != own:
            return jsonify({"error": "Cross-origin request refused"}), 403
        if fetch_site == "cross-site":
            return jsonify({"error": "Cross-site request refused"}), 403

# ── Configuration ──────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("VIDCOL_DATA_DIR") or (Path(__file__).resolve().parent / "data"))
CONFIG_FILE = DATA_DIR / "config.json"
PLAYLISTS_FILE = DATA_DIR / "playlists.json"
THEATER_FILE = DATA_DIR / "theater.json"
FOLDER_LAYOUTS_FILE = DATA_DIR / "folder_layouts.json"
CLIP_NAMES_FILE = DATA_DIR / "clip_names.json"  # in-app display labels keyed by clip path (disk files untouched)
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v"}

# Bento tile column spans (theater grid is 12 columns; height follows clip aspect)
THEATER_MIN_COLS, THEATER_MAX_COLS = 2, 12

# Remux cache: stores browser-compatible copies of videos with incompatible codecs
REMUX_DIR = DATA_DIR / "remux_cache"
REMUX_DIR.mkdir(exist_ok=True)

# Legacy default path (used only for first-run migration if no config exists)
_LEGACY_MEDIA_ROOT = None

# Ensure data directory exists
DATA_DIR.mkdir(exist_ok=True)


def _load_json(path: Path, default=None):
    if default is None:
        default = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return default
    return default


def _save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Config Management ────────────────────────────────────────────
def _load_config():
    """Load config. Migrates from hardcoded path on first run."""
    if CONFIG_FILE.exists():
        cfg = _load_json(CONFIG_FILE, {
            "mediaPaths": [],
            "excludedFolders": ["Scripts", "scripts"]
        })
        # Ensure branding fields exist (backward compat for existing installs)
        cfg.setdefault("siteName", "My Collection")
        cfg.setdefault("theaterName", "My Theater")
        return cfg

    # First run: migrate from legacy hardcoded path
    config = {
        "siteName": "My Collection",
        "theaterName": "My Theater",
        "mediaPaths": [],
        "excludedFolders": ["Scripts", "scripts"]
    }
    if _LEGACY_MEDIA_ROOT and Path(_LEGACY_MEDIA_ROOT).exists():
        config["mediaPaths"].append({
            "path": str(_LEGACY_MEDIA_ROOT),
            "name": "My Videos"
        })
    _save_json(CONFIG_FILE, config)
    return config


def _save_config(config):
    _save_json(CONFIG_FILE, config)


def _get_media_roots():
    """Get list of configured media root paths."""
    config = _load_config()
    return config.get("mediaPaths", [])


def _get_excluded():
    """Get set of excluded folder names."""
    config = _load_config()
    return set(config.get("excludedFolders", []))


def _get_hidden():
    """Get set of hidden folder keys (sourceIndex:folderName)."""
    config = _load_config()
    return set(config.get("hiddenFolders", []))


def _is_contained(candidate: Path, root: Path) -> bool:
    """Return True iff candidate, after symlink resolution, sits inside root."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _resolve_video_path(relative_path):
    """Search all media roots for a relative video path. Returns absolute Path or None.

    Hardened against path traversal: rejects absolute paths and parent-dir segments
    upfront, and confirms every candidate resolves inside its declared root.
    """
    if not relative_path:
        return None
    # Reject absolute paths (POSIX /, Windows \, drive letters like C:)
    if relative_path.startswith(("/", "\\")):
        return None
    if len(relative_path) >= 2 and relative_path[1] == ":":
        return None
    # Reject parent-dir segments — splits on both separators so "foo..bar.mp4" is allowed
    if ".." in relative_path.replace("\\", "/").split("/"):
        return None

    # Check cache directory for cached external videos
    if relative_path.startswith("cache/"):
        cache_candidate = CACHE_DIR / Path(relative_path).name
        if _is_contained(cache_candidate, CACHE_DIR) and cache_candidate.exists() and cache_candidate.is_file():
            return cache_candidate
        return None

    for source in _get_media_roots():
        root = Path(source["path"])
        # Standard join: source_root / folder / filename
        candidate = root / relative_path
        if _is_contained(candidate, root) and candidate.exists() and candidate.is_file():
            return candidate
        # Flat roots (collections, or any root holding videos directly) address their
        # clips as "<root name>/<file>", so strip that leading segment — but ONLY when
        # it actually names this root, else an unrelated folder name could resolve to a
        # same-named file sitting in the root.
        parts = relative_path.split("/", 1)
        if len(parts) == 2 and parts[0] == root.name:
            candidate = root / parts[1]
            if _is_contained(candidate, root) and candidate.exists() and candidate.is_file():
                return candidate
    return None


def _resolve_folder_path(folder_name):
    """Search all media roots for a folder. Returns (absolute Path, source_dict) or (None, None)."""
    for source in _get_media_roots():
        candidate = Path(source["path"]) / folder_name
        if candidate.exists() and candidate.is_dir():
            return candidate, source
    # Flat roots: the source root itself is the browseable folder. Second pass, so a
    # real subfolder of the same name always wins.
    for source in _get_media_roots():
        root = Path(source["path"])
        if root.name == folder_name and root.exists() and root.is_dir():
            return root, source
    return None, None


def _source_folder_entries(idx, source, excluded, hidden):
    """Browseable folder entries for one media source.

    Shared by /api/folders (the sidebar) and /api/sources (the Settings counts) so the
    two can never disagree. A root holding videos directly is itself browseable; a root
    of subfolders exposes those; a root doing both exposes both. Being "flat" is read
    off the filesystem, not off a stored flag, so sources saved without one still work.
    """
    root = Path(source["path"])
    entries = []
    if not root.exists():
        return entries
    try:
        children = sorted(root.iterdir())
    except (OSError, PermissionError):
        return entries

    direct = [f for f in children if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
    if direct:
        entries.append({
            "name": source.get("name", root.name),
            "path": root.name,
            "count": len(direct),
            "source": source.get("name", f"Source {idx}"),
            "sourceIndex": idx,
            "hidden": f"{idx}:{root.name}" in hidden,
            "isCollection": True,
        })
    if source.get("collection"):
        return entries  # collections are flat by design — don't descend

    for item in children:
        if item.is_dir() and item.name not in excluded:
            try:
                vids = [f for f in item.iterdir()
                        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
            except (OSError, PermissionError):
                continue
            if vids:
                entries.append({
                    "name": item.name,
                    "path": item.name,
                    "count": len(vids),
                    "source": source.get("name", f"Source {idx}"),
                    "sourceIndex": idx,
                    "hidden": f"{idx}:{item.name}" in hidden,
                })
    return entries


def _sanitize_filename(title, max_length=120):
    """Strip illegal filesystem chars from a title for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', '', title)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:max_length].strip() if len(name) > max_length else (name or "video")


def _unique_filename(directory, base_name, ext):
    """Return a unique filename in directory, appending (2), (3) on collision."""
    candidate = f"{base_name}{ext}"
    if not (Path(directory) / candidate).exists():
        return candidate
    counter = 2
    while (Path(directory) / f"{base_name} ({counter}){ext}").exists():
        counter += 1
    return f"{base_name} ({counter}){ext}"


def _sanitize_label(name, max_length=200):
    """Clean a user-supplied display label. Unlike a filename, a label never touches the
    filesystem, so ':' '/' '?' are allowed — we only drop control chars and cap length."""
    cleaned = "".join(ch for ch in (name or "") if ch.isprintable())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length].strip()


def _load_clip_names():
    """Map of {relative_path: display_label}. Disk files are never renamed; this is the label store."""
    return _load_json(CLIP_NAMES_FILE, {})


def _apply_clip_names(clips, names=None):
    """Overwrite each clip's `name` with its saved display label, if one exists (in place)."""
    if names is None:
        names = _load_clip_names()
    for clip in clips:
        label = names.get(clip.get("path"))
        if label:
            clip["name"] = label
    return clips


def _theater_json(data):
    """Single exit point for any response carrying theater clips.

    theater.json stores whatever name a clip had when it was added; renaming only
    writes clip_names.json. So EVERY response built from theater.json must re-apply
    labels or it silently reverts them — route responses included, not just the GET.
    """
    _apply_clip_names(data.get("clips", []))
    return jsonify(data)


def _resolve_folder_for_source(folder_name, source_idx):
    """Resolve a folder path given folder name and source index."""
    sources = _get_media_roots()
    if source_idx is not None:
        try:
            root = Path(sources[int(source_idx)]["path"])
            # For collections, videos sit in the root itself
            if sources[int(source_idx)].get("collection"):
                if root.exists() and root.is_dir():
                    return root
            candidate = root / folder_name
            if candidate.exists() and candidate.is_dir():
                return candidate
        except (IndexError, ValueError):
            pass
    # Fallback
    folder_path, _ = _resolve_folder_path(folder_name)
    return folder_path


# ── API: Folder Browser ──────────────────────────────────────────
@app.route("/api/browse-folders")
def browse_folders():
    """Browse filesystem directories for the folder picker."""
    import platform
    requested = request.args.get("path", "").strip()

    # If no path requested, return starting locations
    if not requested:
        roots = []
        if platform.system() == "Windows":
            # List available drive letters
            import string
            for letter in string.ascii_uppercase:
                drive = f"{letter}:\\"
                if Path(drive).exists():
                    roots.append({"name": f"{letter}:", "path": drive})
        else:
            # macOS / Linux: show home + common locations
            home = Path.home()
            roots.append({"name": "Home", "path": str(home)})
            for sub in ["Documents", "Downloads", "Desktop", "Movies", "Videos"]:
                p = home / sub
                if p.exists():
                    roots.append({"name": sub, "path": str(p)})
            if Path("/Volumes").exists():
                for vol in sorted(Path("/Volumes").iterdir()):
                    if vol.is_dir():
                        roots.append({"name": vol.name, "path": str(vol)})
        return jsonify({"path": "", "parent": None, "dirs": roots})

    target = Path(requested)
    if not target.exists() or not target.is_dir():
        return jsonify({"error": "Path not found"}), 404

    # Get parent path
    parent = str(target.parent) if target.parent != target else None

    # List subdirectories (skip hidden and system folders)
    dirs = []
    try:
        for item in sorted(target.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                try:
                    # Quick permission check
                    list(item.iterdir())
                    dirs.append({"name": item.name, "path": str(item)})
                except PermissionError:
                    pass
    except PermissionError:
        return jsonify({"path": str(target), "parent": parent, "dirs": [], "error": "Permission denied"})

    return jsonify({"path": str(target), "parent": parent, "dirs": dirs})


# ── API: Sources Management ──────────────────────────────────────
@app.route("/api/sources", methods=["GET"])
def get_sources():
    """Get all configured media source paths."""
    config = _load_config()
    sources = config.get("mediaPaths", [])
    excluded = _get_excluded()
    hidden = _get_hidden()
    result = []
    for i, src in enumerate(sources):
        root = Path(src["path"])
        # Count what the sidebar actually shows — same helper, so they can't drift
        entries = _source_folder_entries(i, src, excluded, hidden)
        result.append({
            "index": i,
            "name": src.get("name", f"Source {i}"),
            "path": src["path"],
            "exists": root.exists(),
            "folders": len(entries),
            "videos": sum(e["count"] for e in entries),
        })
    return jsonify({"sources": result})


@app.route("/api/sources", methods=["POST"])
def add_source():
    """Add a new media source path."""
    body = request.json
    path = body.get("path", "").strip()
    name = body.get("name", "").strip()

    if not path:
        return jsonify({"error": "Path is required"}), 400

    source_path = Path(path)
    if not source_path.exists():
        return jsonify({"error": f"Path does not exist: {path}"}), 400
    if not source_path.is_dir():
        return jsonify({"error": "Path is not a directory"}), 400

    if not name:
        name = source_path.name

    config = _load_config()
    # Check for duplicate paths (case-insensitive on Windows)
    existing_paths = [s["path"].lower().replace("\\", "/") for s in config.get("mediaPaths", [])]
    if path.lower().replace("\\", "/") in existing_paths:
        return jsonify({"error": "Source already exists"}), 409

    config.setdefault("mediaPaths", []).append({"path": path, "name": name})
    _save_config(config)
    return jsonify({"status": "added", "sources": config["mediaPaths"]})


@app.route("/api/sources/<int:index>", methods=["DELETE"])
def remove_source(index):
    """Remove a media source by index."""
    config = _load_config()
    sources = config.get("mediaPaths", [])
    if index < 0 or index >= len(sources):
        return jsonify({"error": "Invalid source index"}), 400

    removed = sources.pop(index)
    config["mediaPaths"] = sources
    _save_config(config)
    return jsonify({"status": "removed", "removed": removed, "sources": sources})


# ── API: Collections ────────────────────────────────────────────
@app.route("/api/collections", methods=["POST"])
def create_collection():
    """Create a new collection folder on disk and auto-add as media source."""
    body = request.json
    parent_path = body.get("path", "").strip()
    name = body.get("name", "").strip()

    if not name:
        return jsonify({"error": "Collection name is required"}), 400
    if not parent_path:
        return jsonify({"error": "Location is required"}), 400

    parent = Path(parent_path)
    if not parent.exists() or not parent.is_dir():
        return jsonify({"error": f"Location does not exist: {parent_path}"}), 400

    collection_path = parent / name
    if collection_path.exists():
        return jsonify({"error": f"Folder already exists: {name}"}), 409

    try:
        collection_path.mkdir(parents=False)
    except OSError as e:
        return jsonify({"error": f"Could not create folder: {str(e)}"}), 500

    # Auto-add as media source with collection flag
    config = _load_config()
    existing_paths = [s["path"].lower().replace("\\", "/") for s in config.get("mediaPaths", [])]
    col_str = str(collection_path)
    if col_str.lower().replace("\\", "/") not in existing_paths:
        config.setdefault("mediaPaths", []).append({
            "path": col_str,
            "name": name,
            "collection": True,
        })
        _save_config(config)

    source_index = len(config["mediaPaths"]) - 1
    return jsonify({
        "status": "created",
        "source": {"path": col_str, "name": name},
        "sourceIndex": source_index,
    })


# ── API: Folder Download (yt-dlp to specific folder) ───────────
@app.route("/api/folder-download", methods=["POST"])
def folder_download():
    """Download a video via yt-dlp into a specific folder with a readable filename."""
    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp not installed. Run: pip install yt-dlp"}), 501

    body = request.get_json(force=True)
    url = body.get("url", "").strip()
    folder_name = body.get("folder", "").strip()
    source_idx = body.get("sourceIndex")
    custom_name = body.get("name", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not folder_name and source_idx is None:
        return jsonify({"error": "Folder is required"}), 400

    # Resolve target folder
    target_dir = _resolve_folder_for_source(folder_name, source_idx)
    if not target_dir or not target_dir.exists():
        return jsonify({"error": "Target folder not found"}), 404

    # Extract video info first (no download) to get title
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            raw_title = info.get("title", "video")
    except Exception as e:
        return jsonify({"error": f"Could not fetch video info: {str(e)}"}), 400

    # Build filename
    base_name = _sanitize_filename(custom_name or raw_title)
    final_filename = _unique_filename(str(target_dir), base_name, ".mp4")
    output_path = str(target_dir / final_filename)

    # Download — browser-compatible format:
    # Prefer H.264 video + AAC audio (universal browser playback).
    # Falls back to best available if browser-safe codecs aren't offered.
    ydl_opts = {
        "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": output_path.replace(".mp4", ".%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 2 * 1024 * 1024 * 1024,  # 2GB
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        "postprocessor_args": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        # Clean up partial downloads
        for partial in target_dir.glob(f"{base_name}*"):
            if partial.stat().st_size == 0:
                try:
                    partial.unlink()
                except OSError:
                    pass
        return jsonify({"error": f"Download failed: {str(e)}"}), 400

    # Find the downloaded file
    downloaded = target_dir / final_filename
    if not downloaded.exists():
        candidates = list(target_dir.glob(f"{base_name}.*"))
        candidates = [c for c in candidates if c.suffix.lower() in VIDEO_EXTENSIONS]
        if candidates:
            downloaded = candidates[0]
            final_filename = downloaded.name
        else:
            return jsonify({"error": "Download completed but file not found"}), 500

    return jsonify({
        "video": {
            "name": downloaded.stem,
            "filename": final_filename,
            "folder": folder_name,
            "path": f"{folder_name}/{final_filename}",
            "size": downloaded.stat().st_size,
            "ext": downloaded.suffix.lower(),
        }
    })


# ── API: Browse Files (folders + video files for import picker) ──
@app.route("/api/browse-files")
def browse_files():
    """Browse filesystem showing both directories and video files."""
    import platform
    requested = request.args.get("path", "").strip()

    if not requested:
        # Return same starting locations as browse-folders
        return browse_folders()

    target = Path(requested)
    if not target.exists() or not target.is_dir():
        return jsonify({"error": "Path not found"}), 404

    parent = str(target.parent) if target.parent != target else None
    dirs = []
    files = []

    try:
        for item in sorted(target.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                try:
                    list(item.iterdir())  # permission check
                    dirs.append({"name": item.name, "path": str(item)})
                except PermissionError:
                    pass
            elif item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({
                    "name": item.name,
                    "path": str(item),
                    "size": item.stat().st_size,
                })
    except PermissionError:
        return jsonify({"path": str(target), "parent": parent, "dirs": [], "files": []})

    return jsonify({"path": str(target), "parent": parent, "dirs": dirs, "files": files})


# ── API: Folder Import (copy local file into folder) ────────────
@app.route("/api/folder-import", methods=["POST"])
def folder_import():
    """Copy a local video file into a target folder."""
    body = request.json
    source_path = body.get("sourcePath", "").strip()
    folder_name = body.get("folder", "").strip()
    source_idx = body.get("sourceIndex")

    if not source_path:
        return jsonify({"error": "Source file path is required"}), 400

    src = Path(source_path)
    if not src.exists() or not src.is_file():
        return jsonify({"error": "Source file not found"}), 404
    if src.suffix.lower() not in VIDEO_EXTENSIONS:
        return jsonify({"error": "Not a supported video format"}), 400

    # Resolve target folder
    target_dir = _resolve_folder_for_source(folder_name, source_idx)
    if not target_dir or not target_dir.exists():
        return jsonify({"error": "Target folder not found"}), 404

    # Copy with unique name
    final_name = _unique_filename(str(target_dir), src.stem, src.suffix)
    dest = target_dir / final_name

    try:
        shutil.copy2(str(src), str(dest))
    except Exception as e:
        return jsonify({"error": f"Copy failed: {str(e)}"}), 500

    return jsonify({
        "video": {
            "name": dest.stem,
            "filename": final_name,
            "folder": folder_name,
            "path": f"{folder_name}/{final_name}",
            "size": dest.stat().st_size,
            "ext": dest.suffix.lower(),
        }
    })


# ── API: Delete Video from Folder ───────────────────────────────
@app.route("/api/folder-video", methods=["DELETE"])
def delete_folder_video():
    """Delete a video file from a folder."""
    body = request.json
    rel_path = body.get("path", "").strip()

    if not rel_path:
        return jsonify({"error": "Video path is required"}), 400

    abs_path = _resolve_video_path(rel_path)
    if not abs_path or not abs_path.exists():
        return jsonify({"error": "Video not found"}), 404

    try:
        abs_path.unlink()
    except OSError as e:
        return jsonify({"error": f"Could not delete: {str(e)}"}), 500

    # Also remove from theater.json if present
    theater = _load_json(THEATER_FILE, {"clips": []})
    original_len = len(theater["clips"])
    theater["clips"] = [c for c in theater["clips"] if c.get("path") != rel_path]
    if len(theater["clips"]) < original_len:
        _save_json(THEATER_FILE, theater)

    return jsonify({"status": "deleted", "path": rel_path})


@app.route("/api/clip-name", methods=["POST"])
def set_clip_name():
    """Set (or clear) a clip's in-app display label. The file on disk is NEVER renamed —
    the label is stored keyed by the clip's path and overrides `name` wherever the clip
    is listed. An empty/blank name clears the override, reverting to the filename stem."""
    body = request.json or {}
    rel_path = (body.get("path") or "").strip()
    if not rel_path:
        return jsonify({"error": "Path is required"}), 400

    # Validate the path with the same traversal-safe resolver used everywhere else.
    # We don't touch the file, but we refuse to store labels for junk/unresolvable paths.
    abs_path = _resolve_video_path(rel_path)
    if not abs_path:
        return jsonify({"error": "Video not found"}), 404

    label = _sanitize_label(body.get("name", ""))
    names = _load_clip_names()
    if label:
        names[rel_path] = label
        display = label
    else:
        names.pop(rel_path, None)      # clear override
        display = abs_path.stem        # revert to the real filename stem
    _save_json(CLIP_NAMES_FILE, names)
    return jsonify({"path": rel_path, "name": display})


# ── API: Branding ────────────────────────────────────────────────
@app.route("/api/branding", methods=["GET"])
def get_branding():
    """Get custom site and theater names."""
    config = _load_config()
    return jsonify({
        "siteName": config.get("siteName", "My Collection"),
        "theaterName": config.get("theaterName", "My Theater"),
    })


@app.route("/api/branding", methods=["POST"])
def update_branding():
    """Update custom site and theater names."""
    body = request.json
    config = _load_config()
    if "siteName" in body:
        config["siteName"] = body["siteName"].strip() or "My Collection"
    if "theaterName" in body:
        config["theaterName"] = body["theaterName"].strip() or "My Theater"
    _save_config(config)
    return jsonify({
        "siteName": config["siteName"],
        "theaterName": config["theaterName"],
    })


# ── Streaming Services (deep-link launcher tiles) ────────────────
# These tiles OPEN a service in a new browser tab. Nothing is ever embedded:
# Netflix sends `X-Frame-Options: DENY`, Max sends `frame-ancestors 'none'`, and
# even where a frame would load, DRM (Widevine/EME) binds playback licences to the
# service's own origin. Framing them is impossible, not merely difficult.
DEFAULT_STREAMING_SERVICES = [
    {"id": "netflix",     "name": "Netflix",     "url": "https://www.netflix.com/browse",              "accent": "#e50914"},
    {"id": "max",         "name": "Max",         "url": "https://play.max.com",                        "accent": "#8b5cf6"},
    {"id": "disneyplus",  "name": "Disney+",     "url": "https://www.disneyplus.com/home",             "accent": "#0063e5"},
    {"id": "primevideo",  "name": "Prime Video", "url": "https://www.amazon.com/gp/video/storefront",  "accent": "#00a8e1"},
    {"id": "hulu",        "name": "Hulu",        "url": "https://www.hulu.com/hub/home",               "accent": "#1ce783"},
    {"id": "appletv",     "name": "Apple TV+",   "url": "https://tv.apple.com",                        "accent": "#c9c9cf"},
    {"id": "peacock",     "name": "Peacock",     "url": "https://www.peacocktv.com/watch/home",        "accent": "#ffc72c"},
    {"id": "paramount",   "name": "Paramount+",  "url": "https://www.paramountplus.com/home/",         "accent": "#0064ff"},
    {"id": "youtube",     "name": "YouTube",     "url": "https://www.youtube.com",                     "accent": "#ff0033"},
    {"id": "crunchyroll", "name": "Crunchyroll", "url": "https://www.crunchyroll.com",                 "accent": "#f47521"},
    {"id": "twitch",      "name": "Twitch",      "url": "https://www.twitch.tv",                       "accent": "#9146ff"},
]

STREAMING_URL_MAX = 2048
STREAMING_MAX_SERVICES = 100
_SERVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_ACCENT = "#00f0ff"


def _validate_service_url(url):
    """Return (safe_url, None) or (None, reason).

    SECURITY BOUNDARY. The value ends up in an anchor href / window.open() in our own
    origin, so `javascript:` or `data:` here would be script execution inside the app.
    https-only, no embedded credentials, no control characters. Enforced server-side so
    the browser-side check can never be the only gate.
    """
    if not isinstance(url, str):
        return None, "URL must be text"
    url = url.strip()
    if not url:
        return None, "URL is required"
    if len(url) > STREAMING_URL_MAX:
        return None, "URL is too long"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None, "URL contains control characters"
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        hostname = parsed.hostname
        has_creds = bool(parsed.username or parsed.password)
    except ValueError:
        return None, "URL could not be parsed"
    if scheme != "https":
        return None, "Only https:// links are allowed"
    if not hostname:
        return None, "URL needs a hostname"
    if has_creds:
        return None, "URLs with embedded credentials are not allowed"
    return url, None


def _safe_accent(value, fallback=DEFAULT_ACCENT):
    """Six-digit hex only — this value is written into an inline CSS custom property,
    where an arbitrary string would be a CSS-injection vector."""
    if isinstance(value, str) and _ACCENT_RE.match(value.strip()):
        return value.strip().lower()
    return fallback


def _service_id_from_name(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug[:48] or "service"


STREAMING_NAME_MAX = 60


def _normalize_service(entry, label="Entry"):
    """Validate one streaming entry. Returns (service, None) or (None, reason).

    The ONE validator for both directions: POST /api/streaming rejects the whole
    payload on any reason, _streaming_services() drops the entry. So anything a save
    accepts survives the next read, and a hand-edited config.json (a numeric name,
    enabled: "false", ...) can't raise. `id` is None when absent or invalid — POST
    then derives one (_assign_service_ids), the read path drops the entry.
    """
    if not isinstance(entry, dict):
        return None, f"{label} is not an object"
    name = entry.get("name")
    name = _sanitize_label(name, max_length=STREAMING_NAME_MAX) if isinstance(name, str) else ""
    if not name:
        return None, f"{label} needs a name"
    url, err = _validate_service_url(entry.get("url"))
    if err:
        return None, f"{name}: {err}"
    enabled, custom = entry.get("enabled", True), entry.get("custom", False)
    if not isinstance(enabled, bool) or not isinstance(custom, bool):
        return None, f"{name}: enabled and custom must be true or false"
    sid = entry.get("id")
    sid = sid.strip().lower() if isinstance(sid, str) else ""
    return {
        "id": sid if _SERVICE_ID_RE.match(sid) else None,
        "name": name,
        "url": url,
        "accent": _safe_accent(entry.get("accent")),
        "enabled": enabled,
        "custom": custom,
    }, None


def _assign_service_ids(services):
    """Give every entry a unique, valid id, in place, keeping list order.

    Explicit ids are claimed first, so an id-less entry listed before a built-in can't
    take the built-in's id, and derived ids also steer clear of every shipped default
    id, so a custom "Netflix" can't silently replace the real one. The de-dup suffix is
    fitted inside the 48-char id limit — a longer id would be dropped on the next read.
    """
    taken, pending = set(), []
    for svc in services:
        if svc["id"] and svc["id"] not in taken:
            taken.add(svc["id"])
        else:
            pending.append(svc)
    taken.update(d["id"] for d in DEFAULT_STREAMING_SERVICES)
    for svc in pending:
        base = svc["id"] or _service_id_from_name(svc["name"])
        sid, n = base, 2
        while sid in taken:
            suffix = f"-{n}"
            sid = base[:48 - len(suffix)] + suffix
            n += 1
        taken.add(sid)
        svc["id"] = sid


def _streaming_services():
    """Saved list merged with shipped defaults. The single builder for every read.

    Saved entries win (the user may have renamed, re-pointed or hidden one). Defaults
    that aren't present get appended, so upgrades pick up newly shipped services without
    clobbering customisations — and a service the user *hid* stays in the list with
    enabled=False, so it is not resurrected on the next read.

    Entries that fail today's validation (_normalize_service, the same check POST
    applies) are dropped rather than raising: a hand-edited or older config.json must
    never take the app down.
    """
    saved = _load_config().get("streamingServices")
    if not isinstance(saved, list):
        saved = []

    by_id, order = {}, []
    for entry in saved:
        svc, err = _normalize_service(entry)
        if err or svc["id"] is None or svc["id"] in by_id:
            continue
        by_id[svc["id"]] = svc
        order.append(svc["id"])

    for default in DEFAULT_STREAMING_SERVICES:
        if default["id"] not in by_id:
            by_id[default["id"]] = dict(default, enabled=True, custom=False)
            order.append(default["id"])

    return [by_id[sid] for sid in order]


# ── Service icons: fetch once, cache locally, never hotlink ──────
# Icons live in data/ (gitignored) — fetched by THIS install for its own use — so the
# repo ships no third-party brand assets and the app stays offline-capable after the
# first fetch. A user-supplied file always beats the fetched one.
SERVICE_ICONS_DIR = DATA_DIR / "service_icons"        # drop your own <id>.png here — wins
SERVICE_ICONS_AUTO = SERVICE_ICONS_DIR / "auto"       # fetched cache
SERVICE_ICONS_AUTO.mkdir(parents=True, exist_ok=True)

ICON_MAX_BYTES = 2 * 1024 * 1024
ICON_MAX_REDIRECTS = 4
ICON_TIMEOUT = 12                  # per socket operation (connect / one read)
ICON_BUDGET = 20                   # wall-clock seconds for one whole lookup, all hops
ICON_MAX_CANDIDATES = 8            # icon URLs tried per lookup (standard paths always kept)
ICON_READ_CHUNK = 64 * 1024
ICON_MISS_TTL = 24 * 3600          # don't retry a failed lookup for a day
_icon_clock = time.monotonic       # indirection so tests can drive the budget
_ICON_UA = "Mozilla/5.0 (compatible; VideoCollection/2.8; +local)"
_ICON_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/svg+xml": ".svg",
    "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico",
}
_ICON_EXTS = (".png", ".svg", ".webp", ".jpg", ".jpeg", ".ico", ".gif")

ICON_MAX_LINKS = 64               # icon-ish <link> tags parsed per page
ICON_LINK_TAG_MAX = 2048          # a <link ...> longer than this is ignored
_ICON_RELS = {"icon", "apple-touch-icon", "apple-touch-icon-precomposed"}
_LINK_OPEN_RE = re.compile(r"<link\b", re.I)
_ICON_SIZE_RE = re.compile(r"(\d{1,5})x\d{1,5}", re.I)


class _LinkTagParser(HTMLParser):
    """Collects the attributes of <link> start tags. Only ever fed one pre-cut tag."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            fields = {}
            for key, value in attrs:
                fields.setdefault(key, value or "")     # first duplicate wins, as in browsers
            self.links.append(fields)


def _icon_links(html):
    """(rank, href) for each icon <link> in a page, in page order. Linear in len(html).

    The page is fetched from whatever host a service URL names, so it is hostile input.
    HTMLParser is deliberately NOT fed the whole page: on some CPython releases (3.11.9
    measured) an unterminated tag makes it rescan to the end of the input from every
    later '<' — 100K chars of '<a ' took minutes — and parsing holds the GIL, which
    stalls every waitress thread. So each '<link' is cut at the next '>' within
    ICON_LINK_TAG_MAX chars and only that bounded fragment, which always ends in its
    single '>', goes through the parser. Ranking: a declared `sizes` wins; an
    apple-touch icon without one scores 180; a plain icon 2.
    """
    found, parsed, pos = [], 0, 0
    while parsed < ICON_MAX_LINKS:
        m = _LINK_OPEN_RE.search(html, pos)
        if not m:
            break
        end = html.find(">", m.start(), m.start() + ICON_LINK_TAG_MAX)
        if end < 0:
            pos = m.end()
            continue
        pos = end + 1
        fragment = html[m.start():end + 1]
        if "icon" not in fragment.lower():
            continue                         # stylesheet / preload / alternate: skip cheaply
        parsed += 1
        parser = _LinkTagParser()
        try:
            parser.feed(fragment)
            parser.close()
        except Exception:                    # _markupbase asserts on junk such as '<!['
            continue
        for attrs in parser.links:
            rels = attrs.get("rel", "").lower().split()
            href = attrs.get("href", "").strip()
            if not href or not _ICON_RELS.intersection(rels):
                continue
            sizes = [int(sm.group(1)) for sm in
                     (_ICON_SIZE_RE.fullmatch(tok) for tok in attrs.get("sizes", "").split()) if sm]
            if sizes:
                rank = max(sizes)
            else:
                rank = 180 if any(r.startswith("apple-touch-icon") for r in rels) else 2
            found.append((rank, href))
    return found


class _NoRedirect(HTTPRedirectHandler):
    """Refuse automatic redirects so every hop can be re-checked against the SSRF guard."""
    def redirect_request(self, *args, **kwargs):
        return None


# NAT64 prefixes (RFC 6052 well-known, RFC 8215 local-use). Refused outright by
# _is_public_ip, even when the IPv4 in the last 32 bits is public. Both prefixes also sit
# inside ::/8, which `is_reserved` flags on every Python measured (3.11, 3.13), but those
# tables have changed between releases, so the refusal does not lean on them. Cost: on a
# DNS64/NAT64-only network (no IPv4 route) icons from IPv4-only hosts can't be fetched.
_NAT64_NETWORKS = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _embedded_ipv4(ip):
    """IPv4 addresses an IPv6 address really leads to: IPv4-mapped, 6to4 and Teredo
    (server AND client). Empty for IPv4 and for plain IPv6. NAT64 is not unwrapped:
    _is_public_ip refuses it before this is consulted.

    How much of this the flags in _is_public_ip already cover depends on the Python
    release, so each form is unwrapped regardless. Measured on 3.11.9: the flags pass
    6to4 (2002::/16 is_global), so this is what refuses 2002:c0a8:101::1 (192.168.1.1),
    while is_reserved (::/8) refuses every IPv4-mapped address; on 3.13 the flags refuse
    6to4 and follow the IPv4 of a mapped one. Teredo (2001::/32) is inside 2001::/23,
    which both flag is_private, so that branch is defence in depth only.
    """
    if ip.version != 6:
        return []
    found = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    return found


def _is_public_ip(ip):
    """True only for globally routable space.

    Both halves are needed. The flags alone miss CGNAT 100.64.0.0/10 (the range
    Tailscale uses): none of them is set for it, only `is_global` is False. And
    `is_global` alone is True on some Python versions for addresses that must be
    refused (::ffff:100.64.0.1, 64:ff9b::7f00:1). `is_link_local` is what blocks
    169.254.169.254 (cloud metadata); `is_site_local` covers deprecated fec0::/10.
    NAT64 is never public (see _NAT64_NETWORKS). Any other IPv6 address that embeds an
    IPv4 one is only public if that IPv4 is too.
    """
    if ip.version == 6 and any(ip in net for net in _NAT64_NETWORKS):
        return False
    if not ip.is_global:
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified or getattr(ip, "is_site_local", False)):
        return False
    return all(_is_public_ip(v4) for v4 in _embedded_ipv4(ip))


def _host_is_public(hostname):
    """SSRF guard. EVERY address the name resolves to must be public — a name with one
    public and one private A record must not pass.

    Residual risk [LOW]: DNS rebinding between this check and the connect. Closing that
    fully means pinning the resolved IP and connecting to it with an explicit Host
    header; not worth the machinery for a LAN-guarded local app.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            if not _is_public_ip(ipaddress.ip_address(info[4][0])):
                return False
        except ValueError:
            return False
    return True


def _icon_time_left(deadline):
    return float("inf") if deadline is None else deadline - _icon_clock()


ICON_MIN_OP_TIMEOUT = 0.01         # never 0: settimeout(0) would mean non-blocking


def _icon_op_timeout(deadline):
    """Socket timeout for the next blocking call of a lookup: ICON_TIMEOUT, cut to the
    time left. Raises TimeoutError (an OSError) once the deadline has passed."""
    left = _icon_time_left(deadline)
    if left <= 0:
        raise TimeoutError("icon lookup budget spent")
    return max(ICON_MIN_OP_TIMEOUT, min(ICON_TIMEOUT, left))


class _DeadlineSocketMixin:
    """Re-arms the socket timeout from the lookup deadline before every blocking call.

    A socket timeout applies to ONE call, and http.client reads the status line and
    headers (and chunk-size lines and trailers inside a single read1()) through many
    small recv calls — so a server sending one byte per (timeout - epsilon) was never
    cut off, and the status/header phase had no bound at all. SSLSocket.recv_into,
    send and do_handshake each bound their whole call by the timeout set when they
    start, so arming them here bounds TLS too.
    """
    _icon_deadline = None

    def _icon_arm(self):
        self.settimeout(_icon_op_timeout(self._icon_deadline))

    def recv(self, *args, **kwargs):
        self._icon_arm()
        return super().recv(*args, **kwargs)

    def recv_into(self, *args, **kwargs):
        self._icon_arm()
        return super().recv_into(*args, **kwargs)

    def send(self, *args, **kwargs):
        self._icon_arm()
        return super().send(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._icon_arm()
        return super().sendall(*args, **kwargs)


class _DeadlineSocket(_DeadlineSocketMixin, socket.socket):
    """The TCP phase: connect, and the CONNECT exchange when a proxy is configured."""


class _DeadlineSSLSocket(_DeadlineSocketMixin, ssl.SSLSocket):
    """The TLS phase: handshake, request, status line, headers and body."""

    def do_handshake(self, *args, **kwargs):
        self._icon_arm()
        return super().do_handshake(*args, **kwargs)


def _icon_connect(address, deadline):
    """socket.create_connection, but every address shares the lookup deadline instead of
    each getting a full timeout of its own (a name with many unreachable addresses)."""
    host, port = address
    err = OSError(f"no address for {host}")
    for family, socktype, proto, _, sockaddr in socket.getaddrinfo(host, port, 0,
                                                                   socket.SOCK_STREAM):
        timeout = _icon_op_timeout(deadline)          # raises once the budget is spent
        sock = _DeadlineSocket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
        except OSError as e:
            sock.close()
            err = e
            continue
        sock._icon_deadline = deadline
        return sock
    raise err


def _icon_tls_context():
    """Certificate and hostname verification against the system trust store — what
    urllib does by default. Tests swap this for a context that trusts a local cert."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


class _DeadlineHTTPSConnection(HTTPSConnection):
    """HTTPSConnection whose sockets take every timeout from the lookup deadline."""

    def __init__(self, host, *, deadline, context, **kwargs):
        context.sslsocket_class = _DeadlineSSLSocket
        super().__init__(host, context=context, **kwargs)
        self._icon_deadline = deadline
        self._icon_context = context

    def connect(self):
        self.sock = _icon_connect((self.host, self.port), self._icon_deadline)
        if self._tunnel_host:                  # an https proxy (environment / system settings)
            self._tunnel()
        tls = self._icon_context.wrap_socket(
            self.sock, server_hostname=self._tunnel_host or self.host,
            do_handshake_on_connect=False)
        tls._icon_deadline = self._icon_deadline
        self.sock = tls                        # assigned first, so close() reaches it on failure
        tls.do_handshake()


class _DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, deadline):
        super().__init__()
        self._icon_deadline = deadline

    def https_open(self, req):
        return self.do_open(partial(_DeadlineHTTPSConnection, deadline=self._icon_deadline,
                                    context=_icon_tls_context()), req)


def _read_icon_body(resp, deadline):
    """Read at most ICON_MAX_BYTES, checking the deadline between chunks.

    read1() returns after a single socket read, so a server dripping one byte at a time
    is noticed; a plain read(n) keeps waiting until n bytes arrive. None = over budget,
    over size, or truncated.
    """
    read = getattr(resp, "read1", None) or resp.read
    chunks, total = [], 0
    while True:
        if _icon_time_left(deadline) <= 0:
            return None
        chunk = read(min(ICON_READ_CHUNK, ICON_MAX_BYTES + 1 - total))
        if not chunk:
            # http.client returns a short body silently when the connection drops before
            # Content-Length is satisfied; `length` is what was still owed.
            if getattr(resp, "length", None):
                return None
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > ICON_MAX_BYTES:
            return None


def _icon_http_get(url, accept="*/*", deadline=None):
    """GET over https with manual redirect handling. Returns (final_url, ctype, body).

    `deadline` (an _icon_clock() value) bounds the whole call on every hop: connect, TLS
    handshake, request, status line, headers and body (_DeadlineHTTPSConnection gives
    each blocking socket call only the time left). ICON_TIMEOUT alone is per socket
    call, so without it a slow-drip server could hold a worker thread indefinitely.
    Not covered: DNS resolution (getaddrinfo takes no timeout; the OS resolver's own
    timeout bounds it).
    """
    opener = build_opener(_NoRedirect, _DeadlineHTTPSHandler(deadline))
    for _ in range(ICON_MAX_REDIRECTS + 1):
        remaining = _icon_time_left(deadline)
        if remaining <= 0:
            return None
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        if not _host_is_public(parsed.hostname):
            return None
        req = Request(url, headers={"User-Agent": _ICON_UA, "Accept": accept})
        try:
            resp = opener.open(req, timeout=min(ICON_TIMEOUT, remaining))
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urljoin(url, e.headers["Location"])
                continue
            return None
        except (URLError, OSError, ValueError, HTTPException):
            # HTTPException: http.client raises it from getresponse() (bad or HTTP/2
            # status line, too many or overlong headers) and from putrequest() for a path
            # it refuses (InvalidURL, e.g. a space in an icon href) — not an OSError.
            return None
        try:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = _read_icon_body(resp, deadline)
        except (OSError, ValueError, HTTPException):   # timeout / reset / truncated body
            return None
        finally:
            resp.close()
        if not body:
            return None
        return url, ctype, body
    return None


def _icon_candidates(service_url, deadline=None):
    """Icon URLs to try, best-declared-quality first. At most ICON_MAX_CANDIDATES.

    Everything is sorted by rank: a page <link> ranks by its declared `sizes` (a bare
    apple-touch-icon scores 180, a plain icon 2), the standard /apple-touch-icon.png and
    /apple-touch-icon-precomposed.png rank 150 and 149, and /favicon.ico comes last
    unless the page links it higher. The declared sizes are how Netflix (nothing at the
    standard paths) and Peacock (only 32px, but declared) resolve at all.

    The cap only ever drops PAGE links: the two standard paths and /favicon.ico always
    keep their slots, so an icon-heavy page can't crowd them out, and a page declaring
    hundreds of icons can't turn one lookup into hundreds of third-party requests. A URL
    both linked and standard is tried once, at its better rank.
    """
    parsed = urlparse(service_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    favicon = origin + "/favicon.ico"
    standard = [
        (150, origin + "/apple-touch-icon.png"),
        (149, origin + "/apple-touch-icon-precomposed.png"),
        (-1, favicon),
    ]
    ranked = list(standard)

    page = _icon_http_get(origin + "/", accept="text/html,*/*", deadline=deadline)
    if page and page[1].startswith("text/html"):
        html = page[2].decode("utf-8", "ignore")[:400_000]
        for rank, href in _icon_links(html):
            try:
                ranked.append((rank, urljoin(page[0], href)))
            except ValueError:
                continue

    reserved = {url for _, url in standard}
    links_left = ICON_MAX_CANDIDATES - len(reserved)
    seen, ordered = set(), []
    for _, url in sorted(ranked, key=lambda x: -x[0]):
        # data:/http: hrefs would be refused by the fetcher anyway; don't let them
        # use up the candidate budget.
        if url in seen or not url.lower().startswith("https://"):
            continue
        seen.add(url)
        if url not in reserved:
            if links_left <= 0:
                continue
            links_left -= 1
        ordered.append(url)
    return ordered


def _find_service_icon(service_id):
    """User drop-in wins over the fetched cache."""
    for directory in (SERVICE_ICONS_DIR, SERVICE_ICONS_AUTO):
        for ext in _ICON_EXTS:
            candidate = directory / f"{service_id}{ext}"
            if candidate.exists() and candidate.is_file():
                return candidate
    return None


def _icon_miss_marker(service_id):
    return SERVICE_ICONS_AUTO / f"{service_id}.miss"


def _discard_icon_file(path):
    """Delete one file of the fetched cache. False if it was absent or couldn't go.

    On Windows a file another handle holds open (a send_file in flight, an AV scan, a
    sync client) can't be deleted. That is logged and skipped, never raised: POST
    /api/streaming has already saved the config when it clears icons, so a 500 there
    would report a save that succeeded as failed.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        app.logger.warning("Could not remove %s from the icon cache: %s", path.name, e)
        return False
    return True


def _clear_fetched_icon(service_id):
    """Remove the fetched copy and the miss marker. Never the user's drop-in.

    A lookup already running for this id is invalidated first (_icon_commit), without
    waiting for the per-id lock it holds for up to ICON_BUDGET: its result belongs to
    the URL the service had when it started, so it is discarded, not written after this.
    """
    _invalidate_icon_lookups(service_id)
    removed = 0
    for ext in _ICON_EXTS + (".miss",):
        removed += _discard_icon_file(SERVICE_ICONS_AUTO / f"{service_id}{ext}")
    return removed


def _icon_origin(url):
    """The fetched icon depends only on this (see _icon_candidates)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


_icon_locks = {}
_icon_locks_guard = threading.Lock()


def _icon_lock(service_id):
    """One lock per service id, so parallel first requests (every tile asks at once, on
    8 waitress threads) share one fetch instead of each running their own."""
    with _icon_locks_guard:
        return _icon_locks.setdefault(service_id, threading.Lock())


# Per-id generation, bumped by every clear. A lookup reads it before it reads the
# service's URL and writes its result only if it is unchanged, so a save that re-points
# the service (or Refresh Icons) wins over a lookup already in flight. Only ids that
# have been looked up get an entry. Guarded by _icon_commit_lock, which is only ever
# held for one generation check plus the local file write that goes with it.
_icon_generations = {}
_icon_commit_lock = threading.Lock()


def _icon_generation(service_id):
    with _icon_commit_lock:
        return _icon_generations.setdefault(service_id, 0)


def _invalidate_icon_lookups(service_id):
    with _icon_commit_lock:
        if service_id in _icon_generations:
            _icon_generations[service_id] += 1


@contextmanager
def _icon_commit(service_id, generation):
    """Hold while writing a lookup's result (icon or miss marker). Yields False when
    the fetched copy was cleared since `generation` was read: write nothing then."""
    with _icon_commit_lock:
        yield _icon_generations.get(service_id) == generation


def _write_icon_atomically(target, body):
    """Write to a temp file beside `target`, then os.replace() it into place: a reader
    sees the old file or the complete new one, never a half-written icon, and a failed
    write leaves nothing behind. The dot-prefixed temp name never matches a lookup."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fetch_service_icon(service, generation=None):
    """Resolve, download and cache one service's icon. Returns the cached Path or None.

    The whole lookup — homepage, every candidate, every redirect — shares one
    ICON_BUDGET deadline. Running out counts as a miss (negative-cached like any other).
    `generation` must be read (_icon_generation) before `service` was; nothing is
    written if the icon was cleared since (see _icon_commit).
    """
    service_id = service["id"]
    if generation is None:
        generation = _icon_generation(service_id)
    marker = _icon_miss_marker(service_id)
    if marker.exists():
        try:
            if (datetime.now(timezone.utc).timestamp() - marker.stat().st_mtime) < ICON_MISS_TTL:
                return None
        except OSError:
            pass

    deadline = _icon_clock() + ICON_BUDGET
    for url in _icon_candidates(service["url"], deadline=deadline):
        if _icon_time_left(deadline) <= 0:
            break
        got = _icon_http_get(url, accept="image/*,*/*", deadline=deadline)
        if not got:
            continue
        _, ctype, body = got
        ext = _ICON_TYPES.get(ctype)
        if not ext:
            continue                       # content-type allow-list: images only
        target = SERVICE_ICONS_AUTO / f"{service_id}{ext}"
        try:
            with _icon_commit(service_id, generation) as current:
                if not current:
                    return None            # re-pointed or refreshed meanwhile: stale result
                _write_icon_atomically(target, body)
        except OSError:
            return None
        _discard_icon_file(marker)
        return target

    with _icon_commit(service_id, generation) as current:
        if current:
            try:
                marker.write_text("", encoding="utf-8")   # negative cache; retried after ICON_MISS_TTL
            except OSError:
                pass
    return None


# Served-icon digests, cached per path so an unchanged icon isn't re-read on every tile
# render: str(path) -> (metadata key, digest). One entry per icon file, so it stays small.
ICON_DIGEST_SETTLE_NS = 2 * 10**9     # a file changed more recently is hashed on every request
_icon_wall_ns = time.time_ns          # indirection so tests can drive the settle check
_icon_digests = {}
_icon_digests_lock = threading.Lock()


def _icon_file_digest(f, path):
    """sha256 over WHICH file this is (drop-in vs fetched; the extension picks the
    Content-Type) and every byte of it. Leaves `f` at offset 0."""
    h = hashlib.sha256(f"{path.parent.name}/{path.name}\0".encode())
    for chunk in iter(partial(f.read, ICON_READ_CHUNK), b""):
        h.update(chunk)
    f.seek(0)
    return h.hexdigest()[:32]


def _icon_etag(f, path, st):
    """Strong ETag for the open icon file `f` (`st` is its fstat): a digest of the bytes
    served, so it changes whenever they do.

    The digest is cached under the file's size, mtime, ctime, inode and device, but
    equal metadata alone never proves equal bytes: Linux stamps files from a coarse
    kernel clock, so a refetch written within the same tick at the same size (even on
    the inode just freed) looks identical. So, as git does for its index, a digest is
    only cached once the file is ICON_DIGEST_SETTLE_NS old; any later write then stamps
    a newer mtime or ctime than the cached key holds.
    """
    key = (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev)
    with _icon_digests_lock:
        cached = _icon_digests.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    digest = _icon_file_digest(f, path)
    if _icon_wall_ns() - max(st.st_mtime_ns, st.st_ctime_ns) >= ICON_DIGEST_SETTLE_NS:
        with _icon_digests_lock:
            _icon_digests[str(path)] = (key, digest)
    return digest


def _icon_lookup_target(service_id):
    """The service an icon may be FETCHED for: configured and enabled. Hiding a service
    is the documented opt-out of its lookup, and an unknown id never gets a lock. A
    file already on disk (a drop-in, or cached before it was hidden) is still served."""
    return next((s for s in _streaming_services()
                 if s["id"] == service_id and s["enabled"]), None)


@app.route("/api/service-icon/<service_id>")
def service_icon(service_id):
    """Serve a service icon, fetching it on first request. 404 means 'render text only'."""
    if not _SERVICE_ID_RE.match(service_id or ""):
        return jsonify({"error": "Bad service id"}), 400

    path = _find_service_icon(service_id)
    if path is None:
        if _icon_lookup_target(service_id) is None:
            return jsonify({"error": "Unknown or hidden service"}), 404
        with _icon_lock(service_id):
            path = _find_service_icon(service_id)    # a parallel request may have just fetched it
            if path is None:
                # Generation first, then the service: a save that re-points it after this
                # point also bumps the generation, so this result is discarded. Read here,
                # not before the lock, so a request that queued behind a lookup uses the
                # URL saved meanwhile.
                generation = _icon_generation(service_id)
                service = _icon_lookup_target(service_id)
                path = _fetch_service_icon(service, generation) if service else None
    if path is None:
        return jsonify({"error": "No icon found"}), 404

    # One open file for the digest AND the body: a DELETE or a refetch that replaces the
    # file meanwhile can neither 500 this request nor pair new bytes with an old ETag.
    try:
        f = open(path, "rb")
    except OSError:                          # removed by a DELETE since the lookup
        return jsonify({"error": "No icon found"}), 404
    try:
        st = os.fstat(f.fileno())
        etag = _icon_etag(f, path, st)
    except OSError:
        f.close()
        return jsonify({"error": "No icon found"}), 404
    # Revalidate on every use instead of caching for a week: the strong ETag is a digest
    # of the bytes, so a refetch or a newly dropped-in file shows on the next render — no
    # client-side cache-bust — and an unchanged icon costs one 304. No Last-Modified: a
    # one-second date would let If-Modified-Since answer 304 for a same-second refetch.
    response = send_file(f, download_name=path.name, etag=etag)
    if response.status_code == 200:
        response.content_length = st.st_size
    response.headers["Cache-Control"] = "no-cache"
    # A cached icon is a REMOTE body from a user-added https host. An SVG served as
    # image/svg+xml is an active document under direct navigation — script in it would
    # run in our origin, the same primitive _validate_service_url blocks for hrefs.
    # Tiles use <img>, where SVG is inert, so sandboxing costs the feature nothing.
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = "inline; filename=icon"
    return response


@app.route("/api/service-icon/<service_id>", methods=["DELETE"])
def clear_service_icon(service_id):
    """Drop the fetched copy so the next request re-resolves it. Never touches a
    user drop-in — that file is the user's, not ours."""
    if not _SERVICE_ID_RE.match(service_id or ""):
        return jsonify({"error": "Bad service id"}), 400
    return jsonify({"status": "ok", "removed": _clear_fetched_icon(service_id)})


# ── API: Streaming Services ──────────────────────────────────────
@app.route("/api/streaming", methods=["GET"])
def get_streaming():
    """Launcher tiles. `enabled` drives visibility in the Streaming view."""
    return jsonify({"services": _streaming_services()})


@app.route("/api/streaming", methods=["POST"])
def save_streaming():
    """Replace the whole list: covers add, edit, reorder and hide in one write path.

    Rejects the entire payload if any entry is invalid — partial saves would silently
    drop something the user just typed.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    incoming = payload.get("services")
    if not isinstance(incoming, list):
        return jsonify({"error": "services must be a list"}), 400
    if len(incoming) > STREAMING_MAX_SERVICES:
        return jsonify({"error": f"Too many services (max {STREAMING_MAX_SERVICES})"}), 400

    cleaned = []
    for i, entry in enumerate(incoming):
        svc, err = _normalize_service(entry, label=f"Entry {i + 1}")
        if err:
            return jsonify({"error": err}), 400
        cleaned.append(svc)
    _assign_service_ids(cleaned)

    before = {s["id"]: _icon_origin(s["url"]) for s in _streaming_services()}
    config = _load_config()
    config["streamingServices"] = cleaned
    _save_config(config)
    services = _streaming_services()
    # The icon cache is keyed by id. A service re-pointed at another site, or removed
    # (its id may be reused by a later custom entry), must not keep the old site's icon
    # or its miss marker. The user's own drop-in is never touched.
    after = {s["id"]: _icon_origin(s["url"]) for s in services}
    for sid, origin in before.items():
        if after.get(sid) != origin:
            _clear_fetched_icon(sid)
    return jsonify({"services": services})


# ── API: AI Assistant Config (BYOK) ───────────────────────────
import ai_agent


@app.route("/api/ai/config", methods=["GET"])
def get_ai_config():
    """Report assistant status. NEVER returns the key value."""
    config = _load_config()
    key = ai_agent.get_api_key(config)
    return jsonify({
        "available": ai_agent.is_available(),       # SDK installed?
        "configured": bool(key),                    # key present (env or config)?
        "enabled": ai_agent.assistant_enabled(config),
        "model": ai_agent.get_model(config),
        "envKey": bool(os.environ.get("GEMINI_API_KEY", "").strip()),  # key came from env?
    })


@app.route("/api/ai/config", methods=["POST"])
def set_ai_config():
    """Set the BYOK key / enabled / model. Key is write-only (never echoed back)."""
    body = request.get_json(force=True) or {}
    config = _load_config()
    if "geminiApiKey" in body:
        config["geminiApiKey"] = (body.get("geminiApiKey") or "").strip()
    ai = config.get("aiAssistant") or {}
    if "enabled" in body:
        ai["enabled"] = bool(body["enabled"])
    if "model" in body and body["model"]:
        ai["model"] = str(body["model"]).strip()
    config["aiAssistant"] = ai
    _save_config(config)
    key = ai_agent.get_api_key(config)
    return jsonify({
        "available": ai_agent.is_available(),
        "configured": bool(key),
        "enabled": ai.get("enabled", True),
        "model": ai_agent.get_model(config),
        "envKey": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
    })


@app.route("/api/agent", methods=["POST"])
def agent_chat():
    """Run one assistant turn. Returns {reply, ui_commands, refresh}."""
    config = _load_config()
    if not ai_agent.is_available():
        return jsonify({"error": "AI SDK not installed (pip install google-genai).",
                        "needs_setup": True}), 503
    if not ai_agent.get_api_key(config):
        return jsonify({"error": "No Gemini API key configured. Add one in Settings.",
                        "needs_setup": True}), 503
    if not ai_agent.assistant_enabled(config):
        return jsonify({"error": "Assistant is disabled in Settings."}), 403

    body = request.get_json(force=True) or {}
    message = (body.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Empty message."}), 400
    history = body.get("history", [])
    ctx = body.get("context", {})
    try:
        result = ai_agent.run_agent(message, history, ctx, config)
    except Exception as e:
        return jsonify({"error": f"Assistant error: {e}"}), 502
    return jsonify(result)


# ── API: Toggle Folder Visibility ─────────────────────────────
@app.route("/api/folders/toggle-visibility", methods=["POST"])
def toggle_folder_visibility():
    """Toggle a folder's hidden state. Key format: sourceIndex:folderName."""
    data = request.get_json(force=True)
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"error": "Missing folder key"}), 400

    config = _load_config()
    hidden = config.get("hiddenFolders", [])
    if key in hidden:
        hidden.remove(key)
        visible = True
    else:
        hidden.append(key)
        visible = False
    config["hiddenFolders"] = hidden
    _save_config(config)
    return jsonify({"key": key, "visible": visible})


# ── API: Cache External Video (yt-dlp) ───────────────────────
@app.route("/api/cache-external", methods=["POST"])
def cache_external():
    """Download a video via yt-dlp and cache it locally."""
    if not YT_DLP_AVAILABLE:
        return jsonify({"error": "yt-dlp not installed. Run: pip install yt-dlp"}), 501

    body = request.get_json(force=True)
    url = body.get("url", "").strip()
    custom_name = body.get("name", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Deterministic filename from URL hash (detects duplicates)
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]

    # Check if already cached (any extension)
    existing = list(CACHE_DIR.glob(f"{url_hash}.*"))
    if existing:
        cached_file = existing[0]
        name = custom_name or cached_file.stem
        return jsonify({
            "clip": {
                "path": f"cache/{cached_file.name}",
                "name": name,
                "filename": cached_file.name,
                "folder": "Cached",
                "cached": True,
                "sourceUrl": url,
                "loopStart": None, "loopEnd": None,
            }
        })

    # Download with yt-dlp — browser-compatible format
    output_template = str(CACHE_DIR / f"{url_hash}.%(ext)s")
    ydl_opts = {
        "format": "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 2 * 1024 * 1024 * 1024,  # 2GB
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
        "postprocessor_args": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
    except Exception as e:
        # Clean up partial downloads
        for partial in CACHE_DIR.glob(f"{url_hash}.*"):
            try:
                partial.unlink()
            except OSError:
                pass
        return jsonify({"error": f"Download failed: {str(e)}"}), 400

    # Find the downloaded file
    downloaded = list(CACHE_DIR.glob(f"{url_hash}.*"))
    if not downloaded:
        return jsonify({"error": "Download completed but file not found"}), 500

    cached_file = downloaded[0]
    name = custom_name or title

    return jsonify({
        "clip": {
            "path": f"cache/{cached_file.name}",
            "name": name,
            "filename": cached_file.name,
            "folder": "Cached",
            "cached": True,
            "sourceUrl": url,
            "loopStart": None, "loopEnd": None,
        }
    })


# ── API: Folder Tree ──────────────────────────────────────────
@app.route("/api/folders")
def get_folders():
    """Return folder hierarchy with video counts across all sources."""
    sources = _get_media_roots()
    excluded = _get_excluded()
    hidden = _get_hidden()
    tree = []
    for idx, source in enumerate(sources):
        tree.extend(_source_folder_entries(idx, source, excluded, hidden))
    return jsonify(tree)


# ── API: Videos in a Folder ───────────────────────────────────
@app.route("/api/videos/<path:folder>")
def get_videos(folder):
    """Return list of videos in a specific folder."""
    excluded = _get_excluded()
    if folder in excluded:
        return jsonify({"error": "Folder excluded"}), 403

    # Optional source index for disambiguation
    source_idx = request.args.get("source", None)
    sources = _get_media_roots()
    folder_path = None

    if source_idx is not None:
        try:
            src = sources[int(source_idx)]
            root = Path(src["path"])
            # Collections: videos sit in the source root itself
            if src.get("collection") and root.exists() and root.is_dir():
                folder_path = root
            else:
                candidate = root / folder
                if candidate.exists() and candidate.is_dir():
                    folder_path = candidate
                elif root.name == folder and root.exists() and root.is_dir():
                    folder_path = root  # flat root: the source itself is the folder
        except (IndexError, ValueError):
            pass

    if folder_path is None:
        # Fallback: search all roots (backward compatible with existing theater/playlist paths)
        folder_path, _ = _resolve_folder_path(folder)

    if not folder_path or not folder_path.exists():
        return jsonify({"error": "Folder not found"}), 404

    names = _load_clip_names()
    videos = []
    for f in sorted(folder_path.iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            rel = f"{folder}/{f.name}"
            videos.append({
                "name": names.get(rel, f.stem),  # in-app label wins over the filename stem
                "filename": f.name,
                "folder": folder,
                "path": rel,
                "size": f.stat().st_size,
                "ext": f.suffix.lower(),
            })
    return jsonify(videos)


# ── Auto-Remux: detect & fix browser-incompatible codecs ─────
import subprocess as _sp
import threading as _threading

_remux_locks = {}  # path → Lock (prevents concurrent remux of same file)


def _needs_remux(file_path: Path) -> bool:
    """Check if an MP4 has audio codecs browsers can't play (e.g. Opus in MP4)."""
    if file_path.suffix.lower() != ".mp4":
        return False
    try:
        result = _sp.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a", str(file_path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        import json as _json
        data = _json.loads(result.stdout)
        for stream in data.get("streams", []):
            codec = stream.get("codec_name", "").lower()
            # Browsers support AAC, MP3, and FLAC in MP4. Everything else is suspect.
            if codec and codec not in ("aac", "mp3", "flac", "alac"):
                return True
    except Exception:
        pass
    return False


def _get_remux_path(file_path: Path) -> Path:
    """Get the cached remux path for a given source file."""
    # Use hash of absolute path for uniqueness
    path_hash = hashlib.sha256(str(file_path.resolve()).encode()).hexdigest()[:16]
    return REMUX_DIR / f"{path_hash}.mp4"


def _ensure_remuxed(file_path: Path) -> Path:
    """Return a browser-compatible version of the file. Remuxes if needed."""
    remux_path = _get_remux_path(file_path)

    # Already remuxed and source hasn't changed
    if remux_path.exists():
        if remux_path.stat().st_mtime >= file_path.stat().st_mtime:
            return remux_path

    # Check if remux is actually needed
    if not _needs_remux(file_path):
        return file_path  # Original is fine

    # Thread-safe remux (only one remux per file at a time)
    lock_key = str(file_path.resolve())
    if lock_key not in _remux_locks:
        _remux_locks[lock_key] = _threading.Lock()

    with _remux_locks[lock_key]:
        # Double-check after acquiring lock
        if remux_path.exists() and remux_path.stat().st_mtime >= file_path.stat().st_mtime:
            return remux_path

        # Remux: copy video, transcode audio to AAC
        temp_path = remux_path.with_suffix(".tmp.mp4")
        try:
            _sp.run(
                ["ffmpeg", "-y", "-i", str(file_path),
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                 "-movflags", "+faststart", str(temp_path)],
                capture_output=True, timeout=600,  # 10 min max
            )
            if temp_path.exists() and temp_path.stat().st_size > 0:
                temp_path.replace(remux_path)
                _remux_locks.pop(lock_key, None)
                return remux_path
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    return file_path  # Fallback to original if remux failed


# ── API: Stream Video ─────────────────────────────────────────
@app.route("/api/stream/<path:video_path>")
def stream_video(video_path):
    """Stream video with range request support for seeking."""
    file_path = _resolve_video_path(video_path)
    if not file_path:
        return jsonify({"error": "File not found"}), 404

    # Auto-remux if browser-incompatible codecs detected
    file_path = _ensure_remuxed(file_path)

    file_size = file_path.stat().st_size
    content_type = mimetypes.guess_type(str(file_path))[0] or "video/mp4"

    # Handle WMV specifically
    if file_path.suffix.lower() == ".wmv":
        content_type = "video/x-ms-wmv"

    range_header = request.headers.get("Range")
    if range_header:
        match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            def generate():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk_size = min(1024 * 1024, remaining)  # 1MB chunks
                        data = f.read(chunk_size)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            response = Response(
                generate(),
                status=206,
                mimetype=content_type,
                direct_passthrough=True,
            )
            response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            response.headers["Accept-Ranges"] = "bytes"
            response.headers["Content-Length"] = str(length)
            response.headers["Cache-Control"] = "public, max-age=86400"
            return response

    def generate_full():
        with open(file_path, "rb") as f:
            while True:
                data = f.read(1024 * 1024)
                if not data:
                    break
                yield data

    response = Response(
        generate_full(),
        status=200,
        mimetype=content_type,
        direct_passthrough=True,
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(file_size)
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


# ── API: Video Thumbnail (poster frame) ──────────────────────
@app.route("/api/thumbnail/<path:video_path>")
def get_thumbnail(video_path):
    """Return a thumbnail for the video. Generated on first request."""
    file_path = _resolve_video_path(video_path)
    if not file_path:
        return jsonify({"error": "File not found"}), 404

    thumb_dir = DATA_DIR / "thumbnails"
    thumb_dir.mkdir(exist_ok=True)
    safe_name = video_path.replace("/", "_").replace("\\", "_")
    thumb_path = thumb_dir / f"{safe_name}.jpg"

    if not thumb_path.exists():
        # Try to generate with ffmpeg if available
        try:
            import subprocess
            result = subprocess.run(
                [
                    "ffmpeg", "-i", str(file_path),
                    "-ss", "00:00:02", "-vframes", "1",
                    "-vf", "scale=320:-1",
                    "-q:v", "8",
                    str(thumb_path),
                ],
                capture_output=True, timeout=15,
            )
            if result.returncode != 0:
                # Remove any partial output so a truncated JPEG is never
                # served (and browser-cached for 7 days).
                thumb_path.unlink(missing_ok=True)
                return _placeholder_thumb()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            thumb_path.unlink(missing_ok=True)
            return _placeholder_thumb()

    if thumb_path.exists():
        response = send_file(thumb_path, mimetype="image/jpeg", conditional=True)
        response.headers["Cache-Control"] = "public, max-age=604800"
        return response
    return _placeholder_thumb()


def _placeholder_thumb():
    """Return a tiny placeholder JPEG when no thumbnail can be generated.

    The frontend treats a failed image load OR a decoded width <= 1px as
    "no thumbnail" and renders a styled fallback tile — keep this payload
    at most 1x1 so that detection keeps working.
    """
    import base64
    pixel = base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoH"
        "BwYIDAoMCwsKCwsKDA4QEA0OEQ4LCxAWEBETFBUVFQwPFxgWFBgSFBT/2wBDAQME"
        "BAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
        "FBQUFBQUFBQUFBT/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf"
        "/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAA"
        "AAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AKwA//9k="
    )
    response = Response(pixel, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "public, max-age=300"
    return response


# ── API: Theater State ────────────────────────────────────────
@app.route("/api/theater", methods=["GET"])
def get_theater():
    """Get current theater clips (with in-app display labels applied)."""
    return _theater_json(_load_json(THEATER_FILE, {"clips": []}))


@app.route("/api/theater", methods=["POST"])
def update_theater():
    """Add a clip to theater."""
    clip = request.json
    data = _load_json(THEATER_FILE, {"clips": []})
    # Avoid duplicates by path
    existing_paths = {c["path"] for c in data["clips"]}
    if clip["path"] not in existing_paths:
        data["clips"].append(clip)
        _save_json(THEATER_FILE, data)
    return _theater_json(data)


@app.route("/api/theater/<path:video_path>", methods=["DELETE"])
def remove_from_theater(video_path):
    """Remove a clip from theater."""
    data = _load_json(THEATER_FILE, {"clips": []})
    data["clips"] = [c for c in data["clips"] if c["path"] != video_path]
    _save_json(THEATER_FILE, data)
    return _theater_json(data)


@app.route("/api/theater/reorder", methods=["POST"])
def reorder_theater():
    """Reorder theater clips to match the given list of paths (theater tile swap/reorder).

    Full clip objects are preserved (only their order changes). Unknown paths are
    ignored; any existing clips omitted from the list keep their order at the end.
    """
    body = request.json or {}
    order = body.get("paths", [])
    data = _load_json(THEATER_FILE, {"clips": []})
    by_path = {c["path"]: c for c in data["clips"]}
    reordered = [by_path[p] for p in order if p in by_path]
    seen = {p for p in order if p in by_path}
    reordered += [c for c in data["clips"] if c["path"] not in seen]
    data["clips"] = reordered
    _save_json(THEATER_FILE, data)
    return _theater_json(data)


@app.route("/api/theater/layout", methods=["POST"])
def update_theater_layout():
    """Save workspace layout positions for theater clips."""
    body = request.json
    layouts = {item["path"]: item for item in body.get("layouts", [])}

    data = _load_json(THEATER_FILE, {"clips": []})
    for clip in data["clips"]:
        if clip["path"] in layouts:
            layout = layouts[clip["path"]]
            clip["wsLeft"] = layout.get("wsLeft")
            clip["wsTop"] = layout.get("wsTop")
            clip["wsWidth"] = layout.get("wsWidth")
            clip["wsHeight"] = layout.get("wsHeight")
            clip["wsVolume"] = layout.get("wsVolume")
    _save_json(THEATER_FILE, data)
    return _theater_json(data)


@app.route("/api/theater/loop", methods=["POST"])
def update_loop():
    """Update loop settings for a theater clip."""
    body = request.json
    path = body.get("path")
    loop_start = body.get("loopStart")
    loop_end = body.get("loopEnd")

    data = _load_json(THEATER_FILE, {"clips": []})
    for clip in data["clips"]:
        if clip["path"] == path:
            clip["loopStart"] = loop_start
            clip["loopEnd"] = loop_end
            break
    _save_json(THEATER_FILE, data)
    return _theater_json(data)


@app.route("/api/theater/size", methods=["POST"])
def update_theater_size():
    """Set a theater clip's bento column span.

    Only the width is stored — the tile's height is always derived from the clip's
    aspect ratio on the client, which is what keeps the grid perfectly aligned.
    """
    body = request.json or {}
    path = body.get("path")
    cols = body.get("cols")
    # bool is a subclass of int — reject it explicitly
    if isinstance(cols, bool) or not isinstance(cols, int):
        return jsonify({"error": "cols must be an integer"}), 400
    cols = max(THEATER_MIN_COLS, min(THEATER_MAX_COLS, cols))

    data = _load_json(THEATER_FILE, {"clips": []})
    for clip in data["clips"]:
        if clip["path"] == path:
            clip["bentoCols"] = cols
            _save_json(THEATER_FILE, data)
            return _theater_json(data)
    return jsonify({"error": "Clip not in theater"}), 404


# ── API: Folder Layouts (per-folder video popup positions) ────
@app.route("/api/folder-layouts/<path:folder_key>", methods=["GET"])
def get_folder_layout(folder_key):
    """Get saved popup layouts for a folder."""
    data = _load_json(FOLDER_LAYOUTS_FILE, {})
    return jsonify(data.get(folder_key, {}))


@app.route("/api/folder-layouts/<path:folder_key>", methods=["POST"])
def save_folder_layout(folder_key):
    """Save popup layout for a video within a folder."""
    body = request.json
    video_path = body.get("videoPath", "")
    layout = body.get("layout", {})

    data = _load_json(FOLDER_LAYOUTS_FILE, {})
    if folder_key not in data:
        data[folder_key] = {}
    # Merge: one entry carries BOTH the popup geometry and the bento tile size,
    # so a wholesale overwrite would wipe whichever was saved first.
    data[folder_key].setdefault(video_path, {}).update(layout)
    _save_json(FOLDER_LAYOUTS_FILE, data)
    return jsonify({"ok": True})


# ── API: Playlists ────────────────────────────────────────────
@app.route("/api/playlists", methods=["GET"])
def get_playlists():
    """Get all saved playlists (with in-app display labels applied)."""
    data = _load_json(PLAYLISTS_FILE, {"playlists": []})
    names = _load_clip_names()
    for playlist in data.get("playlists", []):
        _apply_clip_names(playlist.get("clips", []), names)
    return jsonify(data)


@app.route("/api/playlists", methods=["POST"])
def save_playlist():
    """Save current theater as a named playlist."""
    body = request.json
    name = body.get("name", "Untitled")
    clips = body.get("clips", [])

    data = _load_json(PLAYLISTS_FILE, {"playlists": []})
    # Update if name exists, else add new
    found = False
    for pl in data["playlists"]:
        if pl["name"] == name:
            pl["clips"] = clips
            found = True
            break
    if not found:
        data["playlists"].append({"name": name, "clips": clips})
    _save_json(PLAYLISTS_FILE, data)
    return jsonify(data)


@app.route("/api/playlists/<name>", methods=["DELETE"])
def delete_playlist(name):
    """Delete a playlist by name."""
    data = _load_json(PLAYLISTS_FILE, {"playlists": []})
    data["playlists"] = [p for p in data["playlists"] if p["name"] != name]
    _save_json(PLAYLISTS_FILE, data)
    return jsonify(data)


@app.route("/api/playlists/<name>/load", methods=["POST"])
def load_playlist(name):
    """Load a playlist into the theater."""
    data = _load_json(PLAYLISTS_FILE, {"playlists": []})
    for pl in data["playlists"]:
        if pl["name"] == name:
            theater_data = {"clips": pl["clips"]}
            _save_json(THEATER_FILE, theater_data)
            return _theater_json(theater_data)
    return jsonify({"error": "Playlist not found"}), 404


# ── API: Health ───────────────────────────────────────────────
@app.route("/api/health")
def health():
    """Lightweight health probe: source availability + UTC timestamp."""
    sources = _get_media_roots()
    online = sum(1 for s in sources if Path(s["path"]).exists())
    return jsonify({
        "status": "ok",
        "sources_total": len(sources),
        "sources_online": online,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ── API: Shutdown ─────────────────────────────────────────────
@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """Gracefully shutdown the server."""
    import threading
    def _shutdown():
        import time
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"status": "shutting down"})


# ── Serve Frontend ────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7777

    config = _load_config()
    sources = config.get("mediaPaths", [])
    site_name = config.get("siteName", "My Collection")

    # Aurora-cyberpunk banner with dynamic width, RGB gradients, and LAN URL.
    # All banner logic lives in banner.py so launch scripts can share it.
    from banner import print_running_banner

    print_running_banner(port=port, sources=sources, site_name=site_name)

    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=8)
