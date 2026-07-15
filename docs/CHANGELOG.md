# Changelog

## v2.5.4 - 2026-07-15
### Added
- **AI: bulk add to the theater** — "add all of these to my Sanctuary" now adds every video in the current folder. `add_to_theater` accepts `'all'`/`'everything'` (the tool description advertises it), dedupes against clips already present, and reports `count`/`skipped`. New `_srv_add_many_to_theater()` does one load+save regardless of clip count instead of rewriting `theater.json` per clip.
- **AI knows your custom theater name** — `theaterName` now rides in the assistant context and the system prompt ("This user calls the theater 'Sanctuary'"), so your own word for it maps to the theater tools and is used in replies. Falls back to "Theater" when unset.

### Fixed
- **AI: `add_to_theater` silently added only the FIRST match** — it resolved `'all'` to every video but then did `matches[0]`, discarding the rest (its sibling `remove_from_theater` always looped correctly). Now adds every match.
- **AI search was blind to in-app labels** — `search_videos` globbed the filesystem and matched filename stems only, so a renamed clip couldn't be found by its label. It now matches the label **or** the original filename (find it either way) and reports the current label as the name.
- **Popup player stopped at the end instead of looping** — the popup `<video>` was the only player missing the `loop` attribute (theater tiles, workspace panels and hover previews all had it). Clicking a clip anywhere (browse grid or Sanctuary tile — both route through `playVideo()`) now loops back to the start when the clip has no A-B loop set. Safe with A-B loops: `setupVideoLoop`'s handler snaps back into the region if playback drifts before `loopStart`.

## v2.5.3 - 2026-07-13
### Added
- **Rename clips (in-app display labels)**: a pencil action on browse cards and Sanctuary/theater tiles turns the clip's name into an inline field (Enter/blur saves, Esc cancels, empty reverts to the filename). Labels are stored in `data/clip_names.json` keyed by the clip's path and applied server-side in `/api/videos`, `/api/theater`, and `/api/playlists` — so the same label shows everywhere the clip appears (browse, theater, workspace title, popup, AI context). New endpoint `POST /api/clip-name` (traversal-safe; validates the path with `_resolve_video_path`; sanitizes the label; caps length at 200). **The file on disk is never renamed** — this is a label overlay, not a filesystem rename. 11 pytest added.

## v2.5.2 - 2026-07-13
### Fixed
- **Workspace stacking**: clicking (or resizing) a panel no longer flattens every other panel's z-index. The old two-level scheme (clicked panel `10`, everyone else `1`) meant all inactive panels tied at `1` and reverted to DOM/build order — so clicking a far-left panel could drop a far-right panel you'd deliberately placed on top *behind* its neighbor. Replaced with a monotonic "bring to front" counter (`topZ`): clicking a panel raises only that panel, and every other panel keeps the stack you arranged. Grabbing a resize corner now also brings its panel forward. Panels still initialize in clip order; stacking order is not persisted across reloads.

## v2.5.1 - 2026-07-10
### Added
- **Drag-to-swap Sanctuary tiles**: grab a clip by its video area and drop it on another to trade places — curate which clips sit at the top of the viewport without free-floating overlap (they stay snapped to the bento grid). Tiles **glide** into their new spots with a FLIP animation — the grid is *not* rebuilt, so videos never reload/flicker. A 5px threshold keeps plain click (open popup) and hover-preview intact; controls/loop inputs are excluded from the grab. Order persists via `POST /api/theater/reorder` and auto-saves to the loaded playlist. Desktop/mouse for now.

## v2.5.0 - 2026-07-10
### Added
- **Bento grid** 🍱: browse and Theater grids now honor each clip's true aspect ratio — portrait (9:16) clips render as tall tiles and landscape tiles tetris-fill around them (`grid-auto-flow: dense` + per-card row spans). Works with `content-visibility: auto` (spans are computed arithmetically and self-correct on reveal via `contentvisibilityautostatechange`).
- **Aspect-aware popup player**: the popup sizes itself to the video's aspect on open and snaps to it after a free resize — black bars can no longer appear. Portrait clips open as tall windows.

### Fixed
- Cached thumbnails (week-long Cache-Control) could complete before the `load` listener attached — the already-complete case is now handled, so aspects apply on warm caches too.

## v2.4.3 - 2026-07-10
### Changed
- Folder toolbar decluttered: Play/Pause/Unmute/Mute All are now icon-only squares (tooltips carry the labels) and "Download URL" is just "Download" — the breadcrumb gets its breathing room back instead of being clipped by the button row. Mobile overflow menu unchanged (it has its own labels).

## v2.4.2 - 2026-07-10
### Fixed
- Stale breadcrumb: switching to Theater/Playlists no longer keeps the previous folder's "Library > folder" crumb in the topbar — the breadcrumb (and mobile folder chip) now sync with the active view via a single `renderBreadcrumb()` helper. Switching back to browse with a folder still open restores its crumb.

## v2.4.1 - 2026-07-10
### Added
- **Hover preview in the Theater**: mousing over a clip plays it muted, exactly like the browse grid — and if the clip has a loop set, the preview jumps into and plays the loop region. Mouse-leave pauses and rewinds to the loop start (or 2s for un-looped clips). Hover never interferes with Play All: clips that are already playing are left untouched.

## v2.4.0 - 2026-07-10
### Added
- **Draggable AI orb + movable/resizable chat panel**: drag the assistant orb anywhere on screen (5px click/drag threshold, clamped to the viewport); the chat panel anchors to the orb and auto-flips above/below and left/right so it always opens fully on-screen. Top-left resize grip on the panel (min 300×280, up to ~90% of the viewport, bottom-right corner pinned). Orb position and panel size persist per-browser in localStorage; double-click the orb to snap back to the default bottom-right corner. Defaults unchanged.

## v2.3.3 - 2026-07-10
### Fixed
- Collapsed sidebar rail alignment: header stacks logo above the toggle (they were crammed side-by-side in 56px), and all rail controls (toggle, nav, settings, shutdown) share a uniform 40px footprint with 18px icons on one centerline.

## v2.3.2 - 2026-07-10
### Fixed
- Cache-busting `?v=` query on styles.css / assistant.css / app.js / assistant.js so frontend updates apply on a normal refresh instead of being pinned by browser heuristic caching. Bump the version in index.html whenever static assets change.

## v2.3.1 - 2026-07-10
### Fixed
- Escape now closes only the topmost visible overlay (single z-order dispatcher) instead of every open layer at once — e.g. folder picker over Settings unwinds one layer per press. Save-playlist, URL, and download-to-folder modals also gained Escape-to-close.

## v2.3.0 - 2026-07-10
### Added
- **Mobile layer**: bottom tab bar (Browse / Theater / Playlists / Settings) with safe-area support, slide-up folder sheet, toolbar overflow menu, and tap-to-expand search at ≤640px; tablets (641–1024px) start with the collapsed sidebar rail; touch devices get always-visible card actions. Workspace mode stays desktop-only.
- **Design tokens**: spacing scale `--space-1..6`, three control heights `--h-sm/md/lg` (28/34/40px), 5-size type scale with tabular numerals; `:focus-visible` rings and `prefers-reduced-motion` support.
- Sticky glass topbar (backdrop blur); `#main` is now the single scroll container with per-view scroll memory.

### Changed
- **Browse grid is dramatically lighter**: cards render lazy `<img>` thumbnails via `/api/thumbnail/` and a preview `<video>` is created only on hover (or pinned by Play All), then torn down — replaces one streaming `<video>` per card (a 100-video folder now costs a few JPEG fetches instead of ~100 open streams).
- Thumbnail responses are cacheable: `Cache-Control: public, max-age=604800` + conditional ETag (304 revalidation); placeholder gets `max-age=300`; partial thumbnails are deleted on ffmpeg failure so broken images can't get cached.
- Empty states recomposed; glow effects now appear only on hover/active/focus states.

### Removed
- Unused lucide CDN stylesheet — the app loads fully offline again.

### Fixed
- QUICK_REFERENCE z-index map corrected (modal overlays are 100/110, not 400) and expanded with the new mobile layers.
- AI assistant `play_all` now pins the lazy grid previews before playing (parity with the Play All button).
- Long folder names ellipsize on folder cards instead of overflowing; horizontal overflow clipped on the scroll container.
- Scroll position resets when entering a folder; cross-view scroll positions are remembered.

## v2.2.1 - 2026-06-22
### Changed
- Default AI model → `gemini-flash-latest` (alias that auto-tracks Google's newest flash model, so the assistant won't break when an older model is retired). Override per-install via `aiAssistant.model`.
### Fixed
- Silenced the benign `google_genai.types` WARNING about non-text `thought_signature` parts (logged whenever `response.text` is read on a thinking-model reply). Errors still surface.

## v2.2.0 - 2026-06-22
### Added
- **AI Assistant (Gemini, BYOK)**: floating-orb glass chat panel to control the app in natural language — set/clear loops, save/load/rename playlists, add/remove clips, download URLs into folders or the theater, open the workspace, switch views, search, and answer library questions.
- Backend agent loop (`ai_agent.py`) using Google Gemini function-calling. API key stays server-side: `GEMINI_API_KEY` env var first, then git-ignored `data/config.json`. End users enter their own key in **Settings → AI Assistant** (BYOK).
- New endpoints: `GET/POST /api/ai/config` (status only — key never returned) and `POST /api/agent`.
- Context-aware references resolved per request and re-validated server-side: "the third clip", names, "all", "this" (the clip open in the player), and "save this playlist" (the currently-loaded playlist).
- Frontend: `static/assistant.js` + `static/assistant.css`; `google-genai` added to requirements (optional import).

### Notes
- Chat text and library names (folder/clip titles) are sent to Google Gemini (one-line notice shown in the panel). No destructive deletes via chat.

## v2.1.0 - 2026-02-21
### Added
- **Video Prefetch Cache**: Background-fetches videos into memory (Blob URLs) while user browses folders or Theater. Workspace opens instantly when all videos are cached. Memory freed automatically on workspace close.
- Dual-path workspace loading: instant play for cached videos, staggered loading fallback for uncached

### Removed
- VLC workspace integration (shelved). WebView2 "airspace problem" makes it impossible to overlay VLC Direct3D windows on top of WebView2's GPU-composited surface. `vlc_manager.py` kept in repo for potential future use.
- `python-vlc` removed from requirements.txt
- VLC detection removed from install.bat
- DesktopApi js_api bridge removed from desktop.py (simplified to plain launcher)

### Changed
- desktop.py simplified to minimal pywebview launcher (no VLC bridge)
- All VLC dual-path branches removed from app.js (pure HTML5 video)

## v2.0.0 - 2026-02-20
### Added
- Custom Collections feature
- Desktop app via pywebview (start_desktop.bat)
- VLC workspace integration attempt (later reverted in v2.1.0)

## v1.0.0 - Initial Release
### Features
- Folder browsing with multi-source media paths
- Video grid with hover preview
- Popup video player with spacebar play/pause
- "My Theater" theater with per-clip A-B loop controls
- Workspace mode: fullscreen draggable/resizable panels
- Playlist save/load with layout persistence
- Settings UI for media sources and branding
- Cross-platform: Windows, Mac, Linux
- Portable: install scripts auto-install dependencies
