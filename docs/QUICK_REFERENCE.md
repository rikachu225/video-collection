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
- 2.0 design tokens: spacing `--space-1..6` (4/8/12/16/24/32px), control heights `--h-sm/md/lg` (28/34/40px), type scale 11/12/13/15/18px, glow only on hover/active/focus
- Responsive tiers: phone ≤640px (bottom tab bar, folder sheet, overflow menu; workspace hidden), tablet 641–1024px (collapsed sidebar rail), desktop >1024px

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
│   ├── theater.json       ← Current Theater clips + loop settings + layout
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
  └── siteName: "My Collection"               ← Customizable via install script or Settings UI
  └── theaterName: "My Theater"         ← Customizable via install script or Settings UI
  └── mediaPaths: [{path, name}, ...]     ← Multiple root folders supported
  └── excludedFolders: ["Scripts", ...]

API Routes:
  GET  /api/folders                      ← Folder tree across all sources
  GET  /api/videos/<folder>?source=N     ← Videos in folder (optional source disambiguation)
  GET  /api/stream/<path>                ← Video streaming with HTTP Range Requests (1MB chunks)
  GET  /api/thumbnail/<path>             ← ffmpeg-generated poster frames
  POST /api/clip-name                     ← Set/clear a clip's in-app display label {path, name} (disk file NOT renamed; empty name reverts to filename)
  GET  /api/branding                      ← Get custom site + theater names
  POST /api/branding                      ← Update custom names {siteName, theaterName}
  GET  /api/streaming                     ← Streaming launcher tiles (defaults merged with user list)
  POST /api/streaming                     ← Replace whole list {services:[...]} (add/edit/reorder/hide)
  GET  /api/service-icon/<id>             ← Service icon; fetches+caches on first hit (404 = text-only tile)
  DEL  /api/service-icon/<id>             ← Clear the FETCHED copy only (never the user's drop-in)
  GET  /api/sources                       ← List configured media roots
  POST /api/sources                      ← Add media root {name, path}
  DEL  /api/sources/<index>              ← Remove media root
  GET  /api/theater                      ← Current Theater clips
  POST /api/theater                      ← Add clip to Theater
  DEL  /api/theater/<path>               ← Remove clip
  POST /api/theater/layout               ← Save workspace panel positions
  POST /api/theater/reorder              ← Reorder clips to given path order (drag-swap)
  POST /api/theater/loop                 ← Set loop start/end on clip
  POST /api/theater/size                 ← Set a clip's bento column span {path, cols} (2-12; height follows aspect)
  GET  /api/playlists                    ← All saved playlists
  POST /api/playlists                    ← Save/update playlist (upsert by name)
  DEL  /api/playlists/<name>             ← Delete playlist
  POST /api/playlists/<name>/load        ← Load playlist into Theater
  GET  /api/health                       ← Health probe (sources online, timestamp)
  POST /api/shutdown                     ← Graceful server shutdown
  GET  /api/ai/config                    ← AI assistant status (NEVER returns the key)
  POST /api/ai/config                    ← Set BYOK Gemini key / enabled / model
  POST /api/agent                        ← Run one assistant turn {message, history, context}
```

### Frontend (app.js) State
```js
state = {
  folders: [],              // All folder metadata from API
  currentFolder: null,      // Currently browsed folder path
  currentVideos: [],        // Videos in current folder
  currentSourceIndex: null, // Which media source the current folder is from
  theaterClips: [],         // Clips in "My Theater"
  playlists: [],            // All saved playlists
  streamingServices: [],    // Launcher tiles {id,name,url,accent,enabled,custom}
  currentView: "browse",   // Active view: "browse" | "theater" | "playlists" | "streaming"
  searchQuery: "",          // Search filter text
  theaterPlaying: false,    // Whether theater playback is active
  workspaceOpen: false,     // Workspace fullscreen mode active
  workspaceClips: [],       // Clips currently in workspace
  workspaceSource: null,    // "theater" or "browse" (where workspace was opened from)
  loadedPlaylistName: null, // Name of loaded playlist (for auto-save)
}
```

### Views
1. **Browse** - Folder grid (home) or video grid (inside folder). Video cards are lazy `<img>` thumbnails from `/api/thumbnail/`; a preview `<video>` is created on hover (or pinned by Play All) and torn down after. Sidebar with folder tree. Click to open popup player.
2. **My Theater** - Multi-video grid with per-clip loop controls (m:ss format). Play All, Pause All, Mute/Unmute All. Add clips from browse view.
3. **Playlists** - Save/load/delete named playlists. Loading a playlist replaces Theater clips.
4. **Workspace** - Fullscreen mode (Browser Fullscreen API). Draggable + resizable panels (4-corner resize handles). Save/restore layout positions. Auto-tiles if no saved layout. Opened from Theater or Browse (any folder).
5. **Streaming** - Launcher tiles that open Netflix/Max/Disney+/etc. in a new tab. Nothing is embedded (see "Streaming Launcher Tiles" below). Managed in Settings > Streaming.

### Multi-Source System
- `data/config.json` stores multiple media root paths
- Settings UI (gear icon in sidebar) to add/remove sources
- Folders grouped by source in sidebar when multiple sources exist
- Path resolution: searches all roots in order, first match wins
- Backward compatible with existing theater/playlist clip paths
- **Flat vs nested roots (v2.5.6)**: `_source_folder_entries()` is the single builder for both `/api/folders` (sidebar) and `/api/sources` (Settings counts). A root holding videos **directly** becomes its own browseable entry (`path` = root's folder name); a root of subfolders exposes those; a root doing both exposes both. Detected from the filesystem at read time — the `collection` flag is still honored (flat, never descends) but no longer required.
- Flat-root clip paths are `"<root folder name>/<file>"`; `_resolve_video_path()` strips that leading segment **only when it names the root**.

### Video Prefetch Cache (app.js)
- Background-fetches videos into Blob URLs while user browses folders/Theater
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

### Thumbnails
- Grid uses `/api/thumbnail/<path>` — ffmpeg poster frame (320px wide), generated on first request
- Cached responses: `Cache-Control: public, max-age=604800` + conditional ETag (304 revalidation)
- Placeholder (no ffmpeg / failure): `max-age=300`; partial files are removed on ffmpeg failure
- Frontend shows a styled fallback tile when the image errors OR decodes ≤1px wide

### Loop System
- Per-clip `loopStart`/`loopEnd` in seconds
- Uses `timeupdate` event listener on video elements
- Format: `m:ss` (e.g., "1:20" = 80 seconds)
- Supports `m:ss`, `h:mm:ss`, and raw seconds
- Theater hover preview (v2.4.1): mouseenter plays muted from the loop start (or 2s if no loop); mouseleave pauses + rewinds. Skips clips already playing (Play All safe)

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

### Bento Tile Sizing (v2.6.0)
- Tile width = whole grid column spans; height is ALWAYS derived from the clip aspect (`bentoSpan`), which is what guarantees alignment. Only the integer span is stored.
- **Theater** = fixed `repeat(12, 1fr)`. Default span comes from `theaterDefaultCols(count)` → 12/6/4/3, which renders identically to the old 1/2/3/4-column layout. **NEVER reintroduce an inline `grid-template-columns` on the theater grid** — it makes track width depend on clip count, so resizing one tile rescales every other tile.
- **Theater resize snaps to WHOLE-TILE multiples** of the default span (`snapTheaterSpan`/`theaterSpanSteps`, v2.6.1) — e.g. base 4 → only 4/8/12. Arbitrary spans leave a strip narrower than one tile, and dense packing can only place a tile where it FITS, so that strip is permanently dead space. Snapping is applied both during the drag AND in `applyBentoCols`, so sizes stored under a different clip count heal on render. Do not "restore" free-form spans.
- **Browse grid is deliberately UNTOUCHED**; a card spans 1–3 whole cards (default 1 = today's card). Do NOT halve the track to get finer steps: `auto-fill` can yield an ODD track count, which narrows every card while tiles-per-row stays the same — the metric that matters is tile WIDTH, not tiles per row.
- `bentoSpan(card, knownWidth)` — pass `knownWidth` during a resize drag; otherwise it measures `clientWidth`, which would be stale mid-drag without a forced reflow per frame.
- **The resize handle must be nested inside the media box** (`.theater-video-wrap` / `.video-thumb`), never a direct child of the tile: `bentoSpan`'s chrome loop sums `offsetHeight` over non-media children, and an absolutely-positioned element still reports its height, so a card-level handle silently inflates every tile's row span.
- Stored spans are clamped to the available track count at render time (narrow viewports) WITHOUT changing the user's stored choice.
- Persistence: `bentoCols` on theater clips (travels with playlists), `tileCols` in `folder_layouts.json` per folder. Layout writes MERGE on both server and client — one entry holds both popup geometry and tile size.

## Streaming Launcher Tiles (v2.8.0)
- **Nothing is embedded, and that is not fixable.** Netflix returns `X-Frame-Options: DENY`; Max returns `frame-ancestors 'none'` — enforced by the *browser*, not the page, so no proxy or flag defeats it. Independently, their playback runs through EME/Widevine, which binds licences to a verified player on an authorised origin: even a frame that loaded gets no key. Circumventing that is DMCA §1201. **Do not re-attempt iframe embedding of Netflix/Max/Disney+/Prime.**
- YouTube, Vimeo and Twitch *do* publish embed endpoints (verified: no framing headers). If in-app embedding is ever added it is a separate feature and needs a `frame-src` CSP allowlist — the app currently sets no CSP.
- `_streaming_services()` is the single builder for reads: saved list wins, missing defaults are appended (so upgrades add new services without wiping customisations). A hidden service stays in the list with `enabled: false`, which is why it isn't resurrected; a service *deleted* from the list re-appears by design. Invalid entries are dropped on read so a hand-edited `config.json` can't crash the app.
- `POST /api/streaming` replaces the **whole list** — add, edit, reorder and hide share one write path, so there is exactly one place URL validation happens. Invalid entry ⇒ the whole payload is rejected (no silent partial save).
- **Validation is the security boundary**: https only, no embedded credentials, no control chars, ≤2048 chars, ≤100 services; accents must be six-digit hex (they land in an inline CSS custom property). A `javascript:`/`data:` URL here would be script execution in the app's own origin — the client-side check is defence in depth, never the only gate.
- Tiles are `<a rel="noopener noreferrer" referrerpolicy="no-referrer" target="_blank">`. Dropping `noopener` hands the opened page `window.opener` and lets it navigate this app away (reverse tabnabbing).
- **No logo assets in the REPO** — icons are fetched at runtime into `data/service_icons/` (gitignored), so the public repo still ships zero third-party marks and the app stays offline-capable after the first fetch. Custom services also get a stable accent hashed from their name, used behind/around the icon.
- **Icon resolution order** (`_icon_candidates`): `/apple-touch-icon.png` → `/apple-touch-icon-precomposed.png` → homepage `<link rel="...icon...">` tags ranked by their `sizes` attribute (bare `apple-touch-icon` scores 180) → `/favicon.ico`. First candidate that returns an allow-listed image type wins. The HTML parse is load-bearing: Netflix serves nothing at the standard paths.
- **User drop-in beats everything**: `data/service_icons/<id>.png` (user) is checked before `data/service_icons/auto/<id>.*` (fetched). Needed for Crunchyroll (serves no discoverable icon) and Prime Video (48px max). **Settings → Refresh Icons** clears the fetched cache and busts the browser's 7-day image cache (`iconBust` → `?t=`), otherwise a newly dropped-in file wouldn't appear for a week.
- **`GET /api/service-icon/<id>`** fetches on first request, 404 = "render text only" (the `<img>` removes itself on error). **`DELETE`** clears only the *fetched* copy and the miss marker — never the user's file.
- **SSRF is the real risk here `[HIGH]`** — the server fetches URLs derived from user-editable entries. `_host_is_public()` resolves the name and requires EVERY returned address to be public (`is_private`/`is_loopback`/`is_link_local`/`is_multicast`/`is_reserved`/`is_unspecified` all refused — `is_link_local` is what blocks `169.254.169.254`). Redirects are handled MANUALLY (`_NoRedirect`) so every hop is re-checked. https-only, 2MB cap, 4 redirects, 12s timeout, image content-type allow-list, 24h negative cache. **Do not "simplify" this by letting urllib follow redirects.**
- Assistant: `open_streaming_service` (UI command) resolves exact name → id → partial name → hostname. The system prompt lists only *enabled* services so the model won't offer one you don't have; a hidden service is still launchable if you name it explicitly.

## z-index Layer Map
```
Topbar (sticky):      20
Overflow menu:        60   (inside #topbar stacking context)
Mobile tab bar:       90   (phone ≤640px only)
Folder sheet:         95
Modal overlays:      100   (.modal-overlay: settings, save-playlist, URL, new-collection, folder-dl)
Import modal:        110
Tooltips:            300
Video popup overlay: 400
Folder picker:       450
Workspace overlay:   500
Workspace toolbar:   510
AI orb / panel:     9000 / 9001
Toast container:    9999
```

## Data Files (Never Commit, User-Specific)
- `data/config.json` - media source paths, branding, AI key, **streaming launcher tiles** (machine-specific)
- `data/theater.json` - current theater state (clips, loops, layouts)
- `data/playlists.json` - saved playlists
- `data/clip_names.json` - in-app display labels keyed by clip path (disk files never renamed; applied to /api/videos, /api/playlists, and ALL theater responses via `_theater_json()`)
- `data/folder_layouts.json` - per-folder, per-clip popup geometry AND bento tile sizes (`tileCols`); saves MERGE, never overwrite
- `data/thumbnails/` - generated poster frames
- `data/service_icons/` - **your own** streaming icons, `<service-id>.png` (drop one in to override)
- `data/service_icons/auto/` - fetched icon cache + `.miss` negative-cache markers

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
- Defaults: siteName="My Collection", theaterName="My Theater"
- Internal code uses "theater" (function names, IDs, routes, variables) — don't rename those

## AI Assistant (Gemini, BYOK)
- Floating cyan orb (default bottom-right) → glass chat panel. Natural-language control: loops, playlists, downloads, theater/playback, search, Q&A.
- **Movable + resizable (v2.4.0)**: drag the orb anywhere (click = open chat, >5px = drag); panel anchors to the orb and auto-flips to stay on-screen; top-left grip resizes the panel. Position/size persist in localStorage (`aiOrbPos`, `aiPanelSize`); double-click orb = reset to default corner.
- **BYOK**: key resolves `GEMINI_API_KEY` env var first, then `geminiApiKey` in git-ignored `data/config.json`. End users paste their own key in **Settings → AI Assistant**; the key is never returned by the API.
- Backend `ai_agent.py` (tool schema, system prompt, Gemini function-calling loop, executors). Frontend `static/assistant.js` + `static/assistant.css`. Model default `gemini-flash-latest` (auto-tracks newest flash; override in `aiAssistant.model`).
- Two tool types: **server actions** (loops/playlists/downloads — reuse existing routes) and **UI commands** (play/pause/mute/workspace/switch-view — run by `app.js` globals). A per-request context snapshot resolves "the third clip", "this" (open clip), and "save this playlist" (loaded playlist); every reference is re-validated server-side.
- **Resolve-by-name → act-by-path**: `resolve_refs()` matches the user's words against the CONTEXT names (which carry in-app labels from `/api/videos` + `/api/theater`), then executors act on the clip's `path`. This is why renamed clips "just work" — and why the AI sees labels without them being on disk.
- **Custom theater name (v2.5.4)**: `theaterName` rides in the context + system prompt, so "add these to my theater" maps to the theater tools. Frontend sends `state.theaterName` from `buildContext()`.
- **Bulk add (v2.5.4)**: `add_to_theater` accepts `'all'`/`'everything'` → adds every video in the current folder via `_srv_add_many_to_theater()` (one load+save, dedupes, reports count/skipped).
- **Model: `gemini-3.6-flash`** (pinned, v2.7.0). Chosen for multi-step tool calling (Terminal-Bench 2.1 78.0 vs 54 for the lite tier, ~17% fewer output tokens). `gemini-flash-latest` auto-tracks releases if you prefer that; override via `aiAssistant.model`.
- **NEVER send function results as `role="tool"`** — newer models 400 with `Role 'tool' is not supported`. Use `role="user"` for the function-response parts (documented shape, works across versions).
- **`applyRefresh()` must run BEFORE queued UI commands** in `assistant.js`. Otherwise a turn like "load playlist X and open the workspace" opens the workspace on the *old* clips.
- **Layout design (v2.7.0)**: `set_tile_size` takes a LIST so the model composes a whole layout in one call; sizes are relative (`small`/`medium`/`large`/`hero`/`full`) and map to whole-tile multiples of `_theater_base_cols(count)`, mirroring `theaterDefaultCols` in app.js. `create_folder` deliberately returns `needs_location` + the available roots rather than guessing a drive.
- **Streaming (v2.8.0)**: `open_streaming_service` launches a service in a new tab; `switch_view` gained `streaming`. `_streaming_names()` injects the *enabled* service names into the prompt so the model only offers configured ones, and the prompt states plainly that these can't play inside the app.
- No destructive deletes via chat. Privacy: chat + library names are sent to Google Gemini.

## Dependencies
```
flask==3.1.3
flask-cors==6.0.0
waitress==3.0.2
pywebview==6.2.1    # Optional: desktop native window mode
yt-dlp==2026.3.17   # Optional: URL video downloads
google-genai        # Optional: AI assistant (Gemini, BYOK)
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
| 6 | Trust user-supplied paths because LAN guard is on | Defense-in-depth: reject `..`/absolute paths AND verify resolved path lives inside an allowed root via `_is_contained()` |
| 7 | Ship static asset changes without bumping `?v=` | Bump the `?v=` query on css/js links in index.html — browsers heuristic-cache unversioned assets |
| 8 | Call `renderTheater()` after mutating one tile | Full rebuild re-creates every `<video>` → all clips reload/flicker. Mutate the DOM in place + FLIP-glide (see `swapTheaterClips`/`removeTheaterClip`). Needs `void grid.offsetHeight` between Invert and Play or transforms strand |
| 9 | `return jsonify(data)` from a route returning theater clips | Use `_theater_json(data)` — theater.json holds the name from when the clip was added, so any raw response reverts in-app labels. Same for adopting a mutation response into `state.theaterClips`: splice locally instead |
| 10 | Compute a count separately from the list it describes | Settings showed `0 folders, 0 videos` for perfectly valid sources because `/api/sources` re-implemented the sidebar's folder scan. Derive both from one helper (`_source_folder_entries()`) so they can't drift |
