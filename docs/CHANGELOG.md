# Changelog

## v2.8.1 - 2026-07-31
### Added
- **Official app icons on the streaming tiles.** The server resolves each service's icon (standard `apple-touch-icon` paths → the homepage's declared `<link rel="icon">` tags, ranked by their `sizes` attribute → `favicon.ico`), downloads it once, and caches it in `data/service_icons/auto/`. Measured result across the 11 built-ins: **10 resolve**, 8 of them at 144–256px. The size-ranked HTML parse matters — Netflix serves *nothing* at the standard paths (only a `<link>` tag), and it lifted Disney+, Peacock and YouTube well above what their bare favicons would have given.
- **Bring your own icon.** Drop a PNG named after the service id into `data/service_icons/` (e.g. `crunchyroll.png`) and it always beats the fetched copy — that's the fix for Crunchyroll (serves no discoverable icon) and Prime Video (48px, soft on a 4K display). A **Refresh Icons** button in Settings clears the fetched cache and busts the browser's 7-day image cache so a file you just dropped in shows up immediately.
- Icons live entirely in `data/` (now gitignored), fetched by *your* install for *your* use — so the public repo still ships **zero third-party brand assets**, and the app stays offline-capable after the first fetch. Nothing is hotlinked at render time.

### Security
- **SSRF guard on the icon fetcher `[HIGH]`.** The server now fetches URLs derived from user-editable service entries, so a custom service pointed at `https://192.168.1.1/` would otherwise have the server request your router from inside the LAN. Every hostname is resolved and **every** address it returns must be public — `is_private`, `is_loopback`, `is_link_local` (this is what blocks the `169.254.169.254` cloud-metadata endpoint), `is_multicast`, `is_reserved` and `is_unspecified` are all refused, IPv4 and IPv6. Redirects are handled manually precisely so each hop is re-checked rather than trusted.
- Fetches are https-only, capped at 2MB and 4 redirects with a 12s timeout, and the response content-type must be on an image allow-list — an HTML or JSON body is discarded, never cached. Cache filenames come from the already regex-validated service id, so there is no path-traversal surface.
- Failed lookups are negatively cached for 24h, so a service with no icon can't cause a fetch storm on every page load.
- Residual `[LOW]`: DNS rebinding between the resolve check and the connect. Closing it fully means pinning the resolved IP and connecting with an explicit `Host` header — not worth the machinery for a LAN-guarded local app, but noted.

### Fixed
- Tiles render text-only when no icon exists (the `<img>` removes itself on error), so a missing logo can never leave a broken-image box. Verified: Crunchyroll degraded cleanly before a drop-in was supplied.

## v2.8.0 - 2026-07-31
### Added
- **Streaming view — launcher tiles for Netflix, Max, Disney+, Prime Video, Hulu, Apple TV+, Peacock, Paramount+, YouTube, Crunchyroll and Twitch.** A fourth top-level view (sidebar + mobile tab bar) that opens a service in a new tab. Tiles are brand-accent glass with the service name in the app's type scale — deliberately **no logo assets**, so the app stays offline-capable (nothing hotlinked), ships no third-party marks in a public repo, and matches the existing aesthetic. Everything is configurable in **Settings › Streaming**: hide any service, reorder with ↑/↓, re-point a URL (send Netflix to a profile, Max to a hub), or add your own. Custom services get a stable accent derived from their name so they don't all render identical cyan.
- **`open_streaming_service` assistant tool.** "Open Netflix", "put on Crunchyroll" launch the service. Matching is exact name → id → partial name → hostname, so "disney" finds Disney+. The system prompt carries the *enabled* service names, so the assistant recommends only what you actually have configured, and is told plainly that these services can't be played inside the app so it never offers to. `switch_view` gained `streaming`.

### Security
- **URL validation is server-side, at a single write path.** `POST /api/streaming` replaces the whole list, so add / edit / reorder / hide all pass through one validator: **https only**, no embedded credentials, no control characters, 2048-char cap, max 100 services. A `javascript:` or `data:` URL here would be script execution in the app's own origin, so the browser-side check is deliberately *not* the only gate. The whole payload is rejected on any bad entry rather than partially saved. `_streaming_services()` (the read path) instead drops invalid entries silently, so a hand-edited `config.json` can never take the app down.
- **Accent colours are validated to six-digit hex.** They land in an inline CSS custom property, where an arbitrary string is a CSS-injection vector.
- **Every tile is an `<a rel="noopener noreferrer" referrerpolicy="no-referrer">`.** Without `noopener` the opened page receives `window.opener` and can navigate this app elsewhere (reverse tabnabbing). A real anchor also keeps middle-click, Ctrl-click and keyboard activation working.

### Notes
- **Netflix, Max and Disney+ cannot be embedded, and this is not a limitation that can be engineered around.** Measured: Netflix returns `X-Frame-Options: DENY`, Max returns `frame-ancestors 'none'` — both enforced by the browser, not the page. Independently, their playback runs through EME/Widevine, which binds licences to a verified player on an authorised origin, so even a frame that *loaded* would get no key. Deliberately circumventing that is DMCA §1201 territory. Hence launcher tiles, not embeds. YouTube, Vimeo and Twitch **do** publish embed endpoints (verified: no framing headers) if in-app embedding is ever wanted — that would be a separate feature with a `frame-src` CSP allowlist.

## v2.7.1 - 2026-07-31
### Added
- **The assistant panel is draggable, not just the orb — and the orb travels with it.** Grab it anywhere on its chrome — header, notice strip, padding — and both move together by the same amount, keeping their spacing, so the button is never left stranded across the screen. Both positions persist per browser. The resize grip, the input, the buttons and the message list are excluded, so typing, sending and selecting text still work. A dragged panel stays where you put it instead of re-anchoring to the orb; double-clicking the orb clears both positions and restores the default corner.
- **Layout requests understand which surface you're looking at.** Asking for a bento while the workspace is open now re-arranges the *workspace panels* (new `bento_workspace` tool) instead of silently restyling the theater grid you can't see. `workspaceOpen` rides in the assistant context so the model can tell the two apart. Verified live: same sentence routes to `bento_workspace` with the workspace open and `set_tile_size` with it closed.

### Fixed
- **Workspace bento laid some portrait clips out as 16:9 with black bars.** Panels for un-prefetched clips render with `preload="none"`, so `videoWidth`/`videoHeight` are still 0 when the layout runs and the code fell back to 16:9 — which is why prefetched clips looked right and the rest didn't. Those panels are now nudged to `preload="metadata"` and the layout re-runs once their real dimensions arrive (debounced). Verified live: late panels corrected themselves from 1.78 to 0.56 automatically. Any manual drag or resize cancels the pending re-layout, so it can never overwrite an arrangement you made yourself.

## v2.7.0 - 2026-07-31
### Added
- **The assistant can design bento layouts.** New `set_tile_size` tool takes a *list*, so a whole look is composed in one call — e.g. "organize this into a Pinterest-style bento with clip 1 as the hero" produces one hero tile, a couple of double-wide tiles for rhythm, and the rest normal. Sizes are relative (`small` = normal, `medium` = 2x, `large`/`hero` = 3x, `full` = whole row) and always resolve to the whole-tile multiples the grid needs (v2.6.1), so an AI-composed layout can't strand dead space.
- **The assistant can create folders.** New `create_folder` tool. If you don't say where, it does **not** guess — it returns the available locations and asks ("Where would you like to create the *Nature* folder?"), then creates it and registers it as a source. Chains with `download`, so "make a folder called X and download this link into it" is one conversation.

### Changed
- **Default model is now `gemini-3.6-flash`** (was the `gemini-flash-latest` alias). This assistant is a multi-step tool-caller and 3.6-flash is substantially stronger there — Terminal-Bench 2.1 78.0 vs 54 for the lite tier — while using ~17% fewer output tokens per task. Override per install via `aiAssistant.model`; set it back to `gemini-flash-latest` to auto-track future releases instead of pinning.

### Fixed
- **`role="tool"` is rejected by newer models.** The function-calling loop returned tool results as `role="tool"`, which `gemini-3.6-flash` refuses with a 400 (`Role 'tool' is not supported`). Results now go back as `role="user"`, the documented shape, which works across model versions. Without this the assistant would have failed on *every* tool call after the model switch.
- **UI commands ran before the data refresh.** A turn like "switch to the Chill playlist and open it in the workspace" returns both a data change and `open_workspace`; the command ran first, so the workspace opened on the *previous* playlist's clips. `applyRefresh()` now runs before the queued UI commands.

## v2.6.3 - 2026-07-30
### Fixed
- **Bento layout dumped most clips into one tiny row.** The row split was count-based: it closed a row once its aspect sum passed `total / R`, but the guard `rows.length < R - 1` meant the final row could never close — so it absorbed *every* remaining clip. The result was a few normal-sized panels on top and a strip of ultra-small ones underneath. Rows are now built by **target height** (the standard justified-gallery approach): clips are added to a row until justifying it to the canvas width would drop it below the target, then the row closes, so no row can absorb the remainder. The target height itself is binary-searched for the tallest value whose stack still fits the canvas. With 12 mixed portrait/landscape/square clips the split is now 6 + 6 with a tallest-to-shortest row ratio of 1.16 (was one normal row plus a tiny strip), and every panel's aspect is still exact. An under-full final row is capped at the target height so it can't balloon.

## v2.6.2 - 2026-07-30
### Changed
- **Workspace layout button is now a bento layout** (tooltip: "Bento Layout"). It used to build a uniform `cols x rows` grid and shrink each clip to fit *inside* its slot, then centre it — so a portrait clip sitting in a landscape-shaped slot left large dead margins on both sides. `autoTileLayout()` now lays panels out in **justified rows**: every panel keeps its exact aspect ratio and each row is scaled so the row spans the canvas width, with panels edge to edge. The row count is chosen by trying every value and keeping the one whose natural height lands closest to filling the canvas; rows are balanced by aspect sum so they come out at similar heights. Measured on a mixed portrait/landscape workspace: 75% of the canvas covered vs ~57% for the old slot-fitting grid, with every panel's aspect exact. Panels stay absolutely positioned, so they remain freely draggable, resizable and overlappable afterwards — this only changes where the button puts them.

## v2.6.1 - 2026-07-30
### Fixed
- **Resizing a theater tile could strand permanent dead space beside it.** Tiles can only be placed where they *fit* — `grid-auto-flow: dense` backfills holes, but a leftover strip narrower than one tile can never be filled by anything, so it stayed empty. Resizing now snaps to **whole-tile multiples** of the current default span (1×, 2×, 3×… a normal tile), which guarantees the space beside an enlarged tile is an exact number of tiles and the neighbours re-pack into it. Measured on a mixed portrait/landscape theater: the reachable widths now cover 79–86% of the grid, and the widths that produced 68–72% are no longer reachable. Sizes saved before this rule (or under a different clip count) snap to the nearest valid width on render, so existing tiles heal themselves. Browse cards were already whole-card steps and are unchanged.

## v2.6.0 - 2026-07-30
### Added
- **Resize tiles by dragging**: grab the `◢` handle in the corner of a theater tile or a browse card and drag to change its width. Resizing is **aspect-locked** — you set the width in whole grid columns and the height follows the clip's true aspect ratio, so tiles stay perfectly aligned and never letterbox or crop. **Enlarging one tile never resizes another**: column tracks are fixed, so the others keep their exact size (they only re-flow position as the grid re-packs). Theater tiles span 2–12 columns; browse cards resize in whole-card steps (1–3 cards wide). Sizes persist per view — theater sizes save to `theater.json` and travel with playlists, like loops and clip order; browse sizes save per folder in `folder_layouts.json`. Desktop only.
- `POST /api/theater/size {path, cols}` — set a theater clip's column span (clamped 2–12; returns through `_theater_json()` so in-app rename labels stay applied).

### Changed
- The theater grid is now a fixed 12 columns, and its old inline per-clip-count column override is gone. That override made column *width* depend on how many clips you had, which meant changing anything rescaled everything — incompatible with independent resizing. The count-based density survives as the **default span** (12/6/4/3 for 1 / ≤4 / ≤9 / 10+ clips), which is pixel-identical to the old 1/2/3/4-column layout because 12 divides evenly by all of them. Adding or removing clips still re-tightens tiles you haven't resized; a tile you have resized keeps your size.
- The browse grid definition is deliberately **unchanged** — a card's default span of 1 is exactly today's card, so the layout is a no-op by construction at every breakpoint.

### Fixed
- Folder layout saves now **merge** instead of replacing, on both the server (`POST /api/folder-layouts/<key>`) and in the client-side cache the grid renders from. Previously a clip's popup window geometry and its tile size destroyed each other in whichever order they were written.

## v2.5.6 - 2026-07-30
### Fixed
- **A source folder holding videos directly was invisible**: the app assumed a media root contains *subfolders*, and only surfaced a root's own videos when the source carried a `collection` flag — which just `create_collection()` ever set. So a folder added via **Settings → Add Source** that holds loose videos produced zero sidebar entries and looked like the add had silently failed. "Flat" is now detected from the filesystem at read time (`_source_folder_entries()`), so sources already saved without the flag start working with no re-adding. A root with both loose videos and subfolders now exposes **both**.
- **Settings always reported `0 folders, 0 videos` for flat sources and collections**: `/api/sources` counted only subdirectories and ignored the `collection` flag entirely, so even a working collection displayed 0/0. Counts are now derived from the same helper that builds the sidebar, so the two can't disagree.
- **Flat roots are now fully browseable**: `/api/videos/<root name>` resolves to the root (with and without a `?source=` index, so the AI assistant's `list_videos` works too), and `_resolve_folder_path()` falls back to matching a root by name — after subfolder matches, so a real subfolder of the same name still wins.
- `[LOW]` **Path-resolution hardening**: `_resolve_video_path()` stripped the leading path segment for *any* collection source, so a request like `SomeOtherFolder/clip.mp4` could resolve to a same-named file in an unrelated collection root. The strip now requires the segment to name that root. Covered by a regression test.

### Changed
- Settings → Add Source row: the path input and **Browse** button are pinned to the same `--h-md` (34px) control-height token with `align-items: stretch`, so their top and bottom edges line up exactly instead of drifting with font metrics. All four controls in that block are now the same height. Hint copy updated to say loose-video folders are supported.

## v2.5.5 - 2026-07-15
### Fixed
- **Removing a theater clip rebuilt the whole grid**: the remove handler called `renderTheater()`, re-creating every `<video>` so all clips re-fetched their streams (the "reload"). Same class of bug as the old drag-to-swap rebuild, fixed the same way — `removeTheaterClip()` fades the tile out (opacity/transform only, so no reflow), then FLIP-glides the survivors into their new spots. Verified: surviving cells and their `<video>` elements are the **same DOM nodes** afterwards, so nothing reloads. Removing the last clip still renders the empty state; a failed delete resyncs via `loadTheater()`.
- **Custom labels reverted on every theater mutation**: only `GET /api/theater` applied clip labels. The add / remove / reorder / loop / layout routes and playlist-load returned `theater.json` raw — and the frontend adopts those responses into `state.theaterClips` — so renaming a clip and then removing *any* clip silently reverted names to filenames (renaming only writes `clip_names.json`; `theater.json` keeps the name from when the clip was added). Every clip-returning response now goes through one `_theater_json()` choke point. The remove handler also splices state locally instead of adopting the response wholesale. 6 pytest added (74 total).

## v2.5.4 - 2026-07-15
### Added
- **AI: bulk add to the theater** — "add all of these to my theater" now adds every video in the current folder. `add_to_theater` accepts `'all'`/`'everything'` (the tool description advertises it), dedupes against clips already present, and reports `count`/`skipped`. New `_srv_add_many_to_theater()` does one load+save regardless of clip count instead of rewriting `theater.json` per clip.
- **AI knows your custom theater name** — `theaterName` now rides in the assistant context and the system prompt ("This user calls the theater by their custom name"), so your own word for it maps to the theater tools and is used in replies. Falls back to "Theater" when unset.

### Fixed
- **AI: `add_to_theater` silently added only the FIRST match** — it resolved `'all'` to every video but then did `matches[0]`, discarding the rest (its sibling `remove_from_theater` always looped correctly). Now adds every match.
- **AI search was blind to in-app labels** — `search_videos` globbed the filesystem and matched filename stems only, so a renamed clip couldn't be found by its label. It now matches the label **or** the original filename (find it either way) and reports the current label as the name.
- **Popup player stopped at the end instead of looping** — the popup `<video>` was the only player missing the `loop` attribute (theater tiles, workspace panels and hover previews all had it). Clicking a clip anywhere (browse grid or theater tile — both route through `playVideo()`) now loops back to the start when the clip has no A-B loop set. Safe with A-B loops: `setupVideoLoop`'s handler snaps back into the region if playback drifts before `loopStart`.

## v2.5.3 - 2026-07-13
### Added
- **Rename clips (in-app display labels)**: a pencil action on browse cards and theater tiles turns the clip's name into an inline field (Enter/blur saves, Esc cancels, empty reverts to the filename). Labels are stored in `data/clip_names.json` keyed by the clip's path and applied server-side in `/api/videos`, `/api/theater`, and `/api/playlists` — so the same label shows everywhere the clip appears (browse, theater, workspace title, popup, AI context). New endpoint `POST /api/clip-name` (traversal-safe; validates the path with `_resolve_video_path`; sanitizes the label; caps length at 200). **The file on disk is never renamed** — this is a label overlay, not a filesystem rename. 11 pytest added.

## v2.5.2 - 2026-07-13
### Fixed
- **Workspace stacking**: clicking (or resizing) a panel no longer flattens every other panel's z-index. The old two-level scheme (clicked panel `10`, everyone else `1`) meant all inactive panels tied at `1` and reverted to DOM/build order — so clicking a far-left panel could drop a far-right panel you'd deliberately placed on top *behind* its neighbor. Replaced with a monotonic "bring to front" counter (`topZ`): clicking a panel raises only that panel, and every other panel keeps the stack you arranged. Grabbing a resize corner now also brings its panel forward. Panels still initialize in clip order; stacking order is not persisted across reloads.

## v2.5.1 - 2026-07-10
### Added
- **Drag-to-swap theater tiles**: grab a clip by its video area and drop it on another to trade places — curate which clips sit at the top of the viewport without free-floating overlap (they stay snapped to the bento grid). Tiles **glide** into their new spots with a FLIP animation — the grid is *not* rebuilt, so videos never reload/flicker. A 5px threshold keeps plain click (open popup) and hover-preview intact; controls/loop inputs are excluded from the grab. Order persists via `POST /api/theater/reorder` and auto-saves to the loaded playlist. Desktop/mouse for now.

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
