# Changelog

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
