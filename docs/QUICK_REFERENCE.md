# Video Collection - Quick Reference

## 30-Second Overview
- **What**: Portable web-based media center for browsing, playing, and managing personal video collections with workspace mode for multi-video layouts on 4K displays
- **Brand**: Customizable site name and theater name (set during install or in Settings)
- **Tech**: Python 3 + Flask + Waitress (backend), Vanilla JS SPA (frontend)
- **Status**: Production-ready
- **Port**: `http://localhost:7777`

## Key Design Principles
- Cyberpunk aesthetic: dark theme, cyan/purple accents, glass effects, subtle glow
- Apple-inspired UX: precision, minimalism, smooth interactions
- Zero external JS dependencies (no React, no jQuery, no build step)
- Portable: zip and move to any machine, run install script

## File Structure
```
Video Collection/
├── server.py              ← Flask backend (all API routes, video streaming)
├── static/
│   ├── index.html         ← Single-page app shell (all views/modals)
│   ├── app.js             ← All frontend logic (~1300 lines)
│   ├── styles.css          ← All styles (~1200 lines)
│   └── favicon.svg        ← Play button icon (cyan-purple gradient)
├── data/
│   ├── config.json        ← Media source paths (user-configurable)
│   ├── theater.json       ← Current Sanctuary clips + loop settings + layout
│   ├── playlists.json     ← Saved playlists (clips + layouts)
│   └── thumbnails/        ← Auto-generated video thumbnails (ffmpeg)
├── docs/
│   └── QUICK_REFERENCE.md ← THIS FILE
├── install.bat            ← Windows setup (auto-installs Python via winget)
├── install.sh             ← Linux/Mac setup (brew/apt)
├── start.bat              ← Windows launcher (cd /d for shell:startup compat)
├── start.sh               ← Linux/Mac launcher
├── desktop.py             ← pywebview native window launcher (optional)
├── start_desktop.bat      ← Windows launcher for desktop mode
├── vlc_manager.py         ← VLC overlay manager (SHELVED - airspace problem)
├── requirements.txt       ← flask, flask-cors, waitress, pywebview, yt-dlp
├── .gitignore             ← venv/, __pycache__/, data/thumbnails/
└── venv/                  ← Python virtual environment (never commit)
```

## Architecture & Data Flow

### Backend (server.py)
```
Config (data/config.json)
  └── siteName: "My Dolls"               ← Customizable via install script or Settings UI
  └── theaterName: "My Sanctuary"         ← Customizable via install script or Settings UI
  └── mediaPaths: [{path, name}, ...]     ← Multiple root folders supported
  └── excludedFolders: ["Scripts", ...]

API Routes:
  GET  /api/folders                      ← Folder tree across all sources
  GET  /api/videos/<folder>?source=N     ← Videos in folder (optional source disambiguation)
  GET  /api/stream/<path>                ← Video streaming with HTTP Range Requests (1MB chunks)
  GET  /api/thumbnail/<path>             ← ffmpeg-generated poster frames
  GET  /api/branding                      ← Get custom site + theater names
  POST /api/branding                      ← Update custom names {siteName, theaterName}
  GET  /api/sources                       ← List configured media roots
  POST /api/sources                      ← Add media root {name, path}
  DEL  /api/sources/<index>              ← Remove media root
  GET  /api/theater                      ← Current Sanctuary clips
  POST /api/theater                      ← Add clip to Sanctuary
  DEL  /api/theater/<path>               ← Remove clip
  POST /api/theater/layout               ← Save workspace panel positions
  POST /api/theater/loop                 ← Set loop start/end on clip
  GET  /api/playlists                    ← All saved playlists
  POST /api/playlists                    ← Save/update playlist (upsert by name)
  DEL  /api/playlists/<name>             ← Delete playlist
  POST /api/playlists/<name>/load        ← Load playlist into Sanctuary
  POST /api/shutdown                     ← Graceful server shutdown
```

### Frontend (app.js) State
```js
state = {
  folders: [],              // All folder metadata from API
  currentFolder: null,      // Currently browsed folder path
  currentVideos: [],        // Videos in current folder
  currentSourceIndex: null, // Which media source the current folder is from
  theaterClips: [],         // Clips in "My Sanctuary"
  playlists: [],            // All saved playlists
  currentView: "browse",   // Active view: "browse" | "theater" | "playlists"
  searchQuery: "",          // Search filter text
  theaterPlaying: false,    // Whether theater playback is active
  workspaceOpen: false,     // Workspace fullscreen mode active
  workspaceClips: [],       // Clips currently in workspace
  workspaceSource: null,    // "theater" or "browse" (where workspace was opened from)
  loadedPlaylistName: null, // Name of loaded playlist (for auto-save)
}
```

### Views
1. **Browse** - Folder grid (home) or video grid (inside folder). Sidebar with folder tree. Hover preview on thumbnails. Click to open popup player.
2. **My Sanctuary** - Multi-video grid with per-clip loop controls (m:ss format). Play All, Pause All, Mute/Unmute All. Add clips from browse view.
3. **Playlists** - Save/load/delete named playlists. Loading a playlist replaces Sanctuary clips.
4. **Workspace** - Fullscreen mode (Browser Fullscreen API). Draggable + resizable panels (4-corner resize handles). Save/restore layout positions. Auto-tiles if no saved layout. Opened from Sanctuary or Browse (any folder).

### Multi-Source System
- `data/config.json` stores multiple media root paths
- Settings UI (gear icon in sidebar) to add/remove sources
- Folders grouped by source in sidebar when multiple sources exist
- Path resolution: searches all roots in order, first match wins
- Backward compatible with existing theater/playlist clip paths

### Video Prefetch Cache (app.js)
- Background-fetches videos into Blob URLs while user browses folders/Sanctuary
- When workspace opens: if all videos are cached, plays all instantly (no stagger)
- Falls back to original staggered loading if not fully cached yet
- `prefetchCache.flush()` called on workspace close — revokes blob URLs, frees memory
- Context-aware: navigating to a different folder/view flushes old cache automatically
- Pure JavaScript (fetch + Blob + URL.createObjectURL) — works on all platforms
- Fetches one video at a time to avoid HDD seek thrashing

### Desktop Mode (Optional)
- `start_desktop.bat` / `python desktop.py` launches pywebview native window
- WebView2 backend on Windows, webkit on Mac/Linux
- Same Flask/waitress server runs in background thread
- No VLC integration (shelved due to WebView2 airspace problem — see vlc_manager.py)

### Workspace Layout Persistence
- Clip objects can have optional `wsLeft`, `wsTop`, `wsWidth`, `wsHeight` fields
- Saved to `theater.json` via Save Layout button (floppy disk icon)
- Auto-saved to loaded playlist when Save Layout is clicked (if a playlist is active)
- Restored on workspace open if layout data exists, otherwise auto-tiles
- Green flash animation on save button confirms save

## Common Commands
```bash
# Windows
install.bat          # First-time setup (creates venv, installs deps, auto-installs Python if missing)
start.bat            # Launch server (opens browser to localhost:7777)

# Linux/Mac
chmod +x install.sh start.sh
./install.sh         # First-time setup
./start.sh           # Launch server

# Manual
python server.py             # Start on default port 7777
python server.py 8080        # Start on custom port
```

## Keyboard Shortcuts
- **Spacebar**: Play/pause video in popup modal or toggle all workspace videos
- **Escape**: Close popup modal, close workspace, close settings

## Important Implementation Details

### Video Streaming
- HTTP Range Request support for seeking (essential for large files)
- 1MB chunk streaming via generators
- WMV files get explicit `video/x-ms-wmv` content type
- Cache-Control: public, max-age=86400

### Loop System
- Per-clip `loopStart`/`loopEnd` in seconds
- Uses `timeupdate` event listener on video elements
- Format: `m:ss` (e.g., "1:20" = 80 seconds)
- Supports `m:ss`, `h:mm:ss`, and raw seconds

### Drag & Resize (Workspace)
- Mousedown on panel (not buttons/handles/video) starts drag
- 4 corner resize handles with per-corner math (tl, tr, bl, br)
- Bounds clamping: 50px minimum visible, top clamped at 0
- Minimum panel size: 200x150px
- z-index management: clicked panel goes to front

### Toast Notifications
- z-index: 9999 (above all overlays including workspace at 500)
- Auto-dismiss after 2.5s with fade animation
- Types: info, success, error (color-coded left border)

## z-index Layer Map
```
Tooltips:          300
Settings overlay:  400
Workspace overlay: 500
Workspace toolbar: 510
Toast container:   9999
```

## Data Files (Never Commit, User-Specific)
- `data/config.json` - media source paths (machine-specific)
- `data/theater.json` - current theater state (clips, loops, layouts)
- `data/playlists.json` - saved playlists
- `data/thumbnails/` - generated poster frames

## Portability & Cross-Platform Transfer
- No hardcoded paths in code (config-driven)
- **Windows → Mac/Linux transfer**: Files may get `root` ownership. Fix with:
  `sudo chown -R $(whoami) "/path/to/Video Collection"`
- `install.bat` auto-installs Python via winget on Windows
- `install.sh` detects Linux/Mac, uses apt/dnf/pacman/brew
- `start.bat` uses `cd /d "%~dp0"` for shell:startup folder compatibility
- `.gitignore` covers venv/, __pycache__/, thumbnails
- When zipping: manually exclude `venv/` folder (gitignore is git-only)

## Branding / Personalization
- Site name and theater name are customizable (stored in `data/config.json`)
- Install scripts prompt the user on first run ("What do you want to call your site?")
- Also changeable anytime via Settings > Personalization in the web UI
- `GET /api/branding` returns current names, `POST /api/branding` updates them
- JS fetches branding on init via `loadBranding()`, applies to all DOM elements via `applyBranding()`
- Defaults: siteName="My Dolls", theaterName="My Sanctuary"
- Internal code uses "theater" (function names, IDs, routes, variables) — don't rename those

## Dependencies
```
flask==3.1.0
flask-cors==5.0.1
waitress==3.0.2
pywebview==5.3.2    # Optional: desktop native window mode
yt-dlp              # Optional: URL video downloads
```
No frontend dependencies. No build step. No npm.

## NEVER DO THESE
| # | DON'T | DO INSTEAD |
|---|-------|------------|
| 1 | VLC overlay windows over WebView2 | WebView2 airspace problem — GPU surfaces fight. Use HTML5 `<video>` |
| 2 | `replace_all` edits on common strings | Use unique context strings; replace_all cascades break subsequent edits |
| 3 | Toast z-index lower than workspace | Toast z-index must be 9999 (above workspace 500) |
| 4 | Hardcode media paths | Use `data/config.json` mediaPaths array |
| 5 | `start.bat` without `cd /d "%~dp0"` | Required for shell:startup folder compatibility |
