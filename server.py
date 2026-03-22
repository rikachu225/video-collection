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
from pathlib import Path
from flask import Flask, jsonify, request, send_file, Response, send_from_directory
from flask_cors import CORS

try:
    import yt_dlp
    YT_DLP_AVAILABLE = True
except ImportError:
    YT_DLP_AVAILABLE = False

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)


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

# ── Configuration ──────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent / "data"
CONFIG_FILE = DATA_DIR / "config.json"
PLAYLISTS_FILE = DATA_DIR / "playlists.json"
THEATER_FILE = DATA_DIR / "theater.json"
FOLDER_LAYOUTS_FILE = DATA_DIR / "folder_layouts.json"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(exist_ok=True)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v"}

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


def _resolve_video_path(relative_path):
    """Search all media roots for a relative video path. Returns absolute Path or None."""
    # Check cache directory for cached external videos
    if relative_path.startswith("cache/"):
        cache_candidate = CACHE_DIR / Path(relative_path).name
        if cache_candidate.exists() and cache_candidate.is_file():
            return cache_candidate
    for source in _get_media_roots():
        candidate = Path(source["path"]) / relative_path
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _resolve_folder_path(folder_name):
    """Search all media roots for a folder. Returns (absolute Path, source_dict) or (None, None)."""
    for source in _get_media_roots():
        candidate = Path(source["path"]) / folder_name
        if candidate.exists() and candidate.is_dir():
            return candidate, source
    return None, None


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
    result = []
    for i, src in enumerate(sources):
        root = Path(src["path"])
        folder_count = 0
        video_count = 0
        if root.exists():
            excluded = _get_excluded()
            for item in root.iterdir():
                if item.is_dir() and item.name not in excluded:
                    vids = [f for f in item.iterdir()
                            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
                    if vids:
                        folder_count += 1
                        video_count += len(vids)
        result.append({
            "index": i,
            "name": src.get("name", f"Source {i}"),
            "path": src["path"],
            "exists": root.exists(),
            "folders": folder_count,
            "videos": video_count,
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

    # Download
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": output_path.replace(".mp4", ".%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 2 * 1024 * 1024 * 1024,  # 2GB
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
    url_hash = hashlib.md5(url.encode()).hexdigest()[:12]

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

    # Download with yt-dlp
    output_template = str(CACHE_DIR / f"{url_hash}.%(ext)s")
    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "max_filesize": 2 * 1024 * 1024 * 1024,  # 2GB
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
        root = Path(source["path"])
        if not root.exists():
            continue

        # Collections: show the root itself as a browseable folder (flat, no subfolders)
        if source.get("collection"):
            videos = [
                f for f in root.iterdir()
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
            ]
            folder_key = f"{idx}:{root.name}"
            tree.append({
                "name": source.get("name", root.name),
                "path": root.name,
                "count": len(videos),
                "source": source.get("name", f"Source {idx}"),
                "sourceIndex": idx,
                "hidden": folder_key in hidden,
                "isCollection": True,
            })
            continue

        # Regular sources: list subfolders with videos
        for item in sorted(root.iterdir()):
            if item.is_dir() and item.name not in excluded:
                videos = [
                    f for f in item.iterdir()
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
                ]
                if videos:
                    folder_key = f"{idx}:{item.name}"
                    tree.append({
                        "name": item.name,
                        "path": item.name,
                        "count": len(videos),
                        "source": source.get("name", f"Source {idx}"),
                        "sourceIndex": idx,
                        "hidden": folder_key in hidden,
                    })
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
        except (IndexError, ValueError):
            pass

    if folder_path is None:
        # Fallback: search all roots (backward compatible with existing theater/playlist paths)
        folder_path, _ = _resolve_folder_path(folder)

    if not folder_path or not folder_path.exists():
        return jsonify({"error": "Folder not found"}), 404

    videos = []
    for f in sorted(folder_path.iterdir()):
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append({
                "name": f.stem,
                "filename": f.name,
                "folder": folder,
                "path": f"{folder}/{f.name}",
                "size": f.stat().st_size,
                "ext": f.suffix.lower(),
            })
    return jsonify(videos)


# ── API: Stream Video ─────────────────────────────────────────
@app.route("/api/stream/<path:video_path>")
def stream_video(video_path):
    """Stream video with range request support for seeking."""
    file_path = _resolve_video_path(video_path)
    if not file_path:
        return jsonify({"error": "File not found"}), 404

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
                return _placeholder_thumb()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return _placeholder_thumb()

    if thumb_path.exists():
        return send_file(thumb_path, mimetype="image/jpeg")
    return _placeholder_thumb()


def _placeholder_thumb():
    """Return a 1x1 transparent pixel as fallback."""
    import base64
    pixel = base64.b64decode(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoH"
        "BwYIDAoMCwsKCwsKDA4QEA0OEQ4LCxAWEBETFBUVFQwPFxgWFBgSFBT/2wBDAQME"
        "BAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
        "FBQUFBQUFBQUFBT/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf"
        "/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAA"
        "AAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AKwA//9k="
    )
    return Response(pixel, mimetype="image/jpeg")


# ── API: Theater State ────────────────────────────────────────
@app.route("/api/theater", methods=["GET"])
def get_theater():
    """Get current theater clips."""
    data = _load_json(THEATER_FILE, {"clips": []})
    return jsonify(data)


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
    return jsonify(data)


@app.route("/api/theater/<path:video_path>", methods=["DELETE"])
def remove_from_theater(video_path):
    """Remove a clip from theater."""
    data = _load_json(THEATER_FILE, {"clips": []})
    data["clips"] = [c for c in data["clips"] if c["path"] != video_path]
    _save_json(THEATER_FILE, data)
    return jsonify(data)


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
    return jsonify(data)


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
    return jsonify(data)


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
    data[folder_key][video_path] = layout
    _save_json(FOLDER_LAYOUTS_FILE, data)
    return jsonify({"ok": True})


# ── API: Playlists ────────────────────────────────────────────
@app.route("/api/playlists", methods=["GET"])
def get_playlists():
    """Get all saved playlists."""
    data = _load_json(PLAYLISTS_FILE, {"playlists": []})
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
            return jsonify(theater_data)
    return jsonify({"error": "Playlist not found"}), 404


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

    print(f"\n  {site_name} Media Center")
    print(f"  ────────────────────────────")
    if sources:
        for i, src in enumerate(sources):
            status = "OK" if Path(src["path"]).exists() else "NOT FOUND"
            print(f"  Source {i}: {src.get('name', 'Unnamed')} [{status}]")
            print(f"           {src['path']}")
    else:
        print(f"  No media sources configured.")
        print(f"  Add sources via Settings in the web UI.")
    print(f"  Server:     http://localhost:{port}")
    print(f"  ────────────────────────────\n")

    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=8)
