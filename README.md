# 🎬 Video Collection

A self-hosted, zero-build web media center for browsing, playing, and managing personal video collections — now with a natural-language **AI assistant** and a **bento-style adaptive grid**. Cyberpunk-themed, with a fullscreen workspace for multi-video layouts and a mobile-friendly layout.

**Free. Private. No cloud. No accounts. No tracking.** *(The AI assistant is optional, bring-your-own-key, and stays off until you add a key — see [Privacy](#-privacy).)*

## ✨ Features

### 🤖 AI Assistant (optional · bring-your-own-key)
- **Control the app by chatting** — e.g. *"loop the third clip from 1:20 to 1:45"*, *"save this as chill mix"*, *"download this link into my Nature folder"*, *"play everything"*, *"open the workspace"*, *"how many videos are in X?"*
- **Powered by Google Gemini** function-calling — the model can only invoke a fixed, allow-listed set of actions (loops, playlists, downloads, theater/playback, search, read-only questions). **No destructive deletes via chat.**
- **Bring your own key (BYOK)** — the API key stays server-side (a `GEMINI_API_KEY` env var, or a git-ignored config file); each user pastes their own in **Settings → AI Assistant**. The key is never returned by the API.
- **Context-aware** — resolves *"the third clip"*, clip names, *"all"*, *"this"* (the clip you have open), and *"save this playlist"* (the one currently loaded); every reference is re-validated server-side.
- **Floating glass chat orb** — drag it anywhere on screen, resize the panel, double-click to reset. Reachable from every view.

### 📁 Browse & Play
- **Folder-based navigation** — browse your library organized by folders
- **Bento grid** — tiles honor each clip's true aspect ratio: portrait (9:16) clips render as tall tiles and landscape tiles tetris-fill the gaps around them (dense masonry) — no black bars, no wasted space
- **Hover preview** — tiles auto-play muted on hover. Cards render as lazy thumbnails and only spin up a live `<video>` on hover, so a 100-video folder costs a few JPEG fetches instead of 100 open streams
- **Aspect-aware popup player** — click a clip to watch in a draggable, resizable window that sizes itself to the video (portrait opens tall; resizing snaps to the video's shape, so it's never letterboxed)
- **Download from a URL** — paste a YouTube / TikTok / X / direct link and download it straight into a folder or the theater (via `yt-dlp`, transcoded to browser-friendly H.264/AAC)
- **Import & organize** — import local files into folders, create new collections, hide folders, and search instantly
- **Multi-source** — add multiple video directories from Settings

### 🎭 Theater Mode
- **Adaptive bento grid** — the same aspect-honest, gap-filled layout as Browse
- **Drag to reorder** — grab a tile and drop it on another to swap places; tiles **glide** into position (snapped to the grid, never overlapping) so you can curate what sits at the top of your view
- **Loop-aware hover previews** — hover a clip to preview it muted; if it has a loop set, the preview plays the loop region
- **Per-clip loop controls** — custom loop start/end times (m:ss format)
- **Play All / Pause All / Mute All** — global controls
- **Add clips from anywhere** — browse view → add to theater

### 🖥️ Workspace Mode
- **Fullscreen multi-panel layout** — drag and resize video panels freely
- **4-corner resize handles** — precision layout control
- **Save/restore layouts** — panel positions persist across sessions
- **Auto-tile** — intelligent default layout if no saved positions
- **Video prefetch cache** — background-loads videos for instant workspace playback

### 📋 Playlists
- **Save/load named playlists** — organize different video collections
- **Layout persistence** — workspace layouts save with playlists
- **Quick load** — one click to swap your entire theater setup

### 📱 Mobile & Responsive
- **Phone layout** — bottom tab bar, slide-up folder sheet, tap-to-expand search, and a toolbar overflow menu
- **Tablet** — starts with the collapsed sidebar rail
- **Any screen** — the grid reflows to fit; workspace mode is desktop-only

### ⚙️ Fully Customizable
- **Name your app** — personalize the site name and theater name
- **Multi-source management** — add/remove video directories from the UI
- **Exclude / hide folders** — keep specific folders out of browsing
- **Desktop mode** — optional native window via pywebview

## 🚀 Quick Start

### Windows
```bash
git clone https://github.com/rikachu225/video-collection.git
cd video-collection
install.bat
start.bat
```

### Linux / macOS
```bash
git clone https://github.com/rikachu225/video-collection.git
cd video-collection
chmod +x install.sh start.sh
./install.sh
./start.sh
```

The install script will:
1. Install Python if missing (via winget/brew/apt)
2. Create a virtual environment
3. Install dependencies
4. Ask you to personalize your site name and theater name
5. Generate your config

Then open **http://localhost:7777** and add your video folders in Settings.

**Optional — enable the AI assistant:** grab a free [Google Gemini API key](https://aistudio.google.com/apikey), then either set a `GEMINI_API_KEY` environment variable before launching, or paste the key into **Settings → AI Assistant**. The assistant stays disabled until a key is present.

## 📋 Requirements

- **Python 3.10+**
- **ffmpeg** (optional, for thumbnail generation and codec remuxing)
- **yt-dlp** (optional, for URL downloads — installed by default)
- **google-genai + a Google Gemini API key** (optional, only for the AI assistant)
- No Node.js. No npm. No build step. No external JS libraries.

## 🏗️ Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3 + Flask + Waitress (production WSGI) |
| Frontend | Vanilla HTML/CSS/JS (zero dependencies, no build) |
| AI | Google Gemini via the `google-genai` SDK (optional, BYOK) |
| Data | JSON files (config, playlists, theater state, layouts) |
| Streaming | HTTP Range Requests (1MB chunks, full seeking support) |
| Downloads | yt-dlp (optional) |
| Desktop | pywebview (optional native window mode) |

## 📁 Project Structure

```
video-collection/
├── server.py              ← Flask backend (API routes + video streaming)
├── ai_agent.py            ← AI assistant: tool schema + Gemini function-calling loop
├── static/
│   ├── index.html         ← Single-page app shell
│   ├── app.js             ← Core frontend logic (browse, theater, workspace, popup)
│   ├── assistant.js       ← AI chat orb + panel
│   ├── styles.css         ← Cyberpunk dark theme
│   ├── assistant.css      ← AI orb + glass panel styles
│   └── favicon.svg        ← Cyan-purple gradient play icon
├── data/
│   ├── config.example.json ← Example config (copy to config.json)
│   ├── config.json        ← Your media paths + BYOK key (git-ignored, auto-generated)
│   ├── theater.json       ← Current theater state (auto-generated)
│   ├── playlists.json     ← Saved playlists (auto-generated)
│   └── folder_layouts.json ← Popup player positions (auto-generated)
├── tests/                 ← pytest suite (backend + AI agent)
├── install.bat / .sh      ← One-click setup
├── start.bat / .sh        ← Launch server
├── desktop.py             ← Optional native window mode
└── requirements.txt       ← Python dependencies
```

## 🎨 Design

- **Cyberpunk aesthetic** — dark theme, cyan/purple accents, glass effects, subtle glow
- **Apple-inspired UX** — precision, minimalism, smooth interactions
- **Bento layout** — aspect-honest, gap-filled grids that adapt to portrait and landscape
- **4K optimized** — designed for high-DPI displays, with a mobile-friendly responsive layout

## ⌨️ Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play/pause video (popup or all workspace videos) |
| `Esc` | Close popup / exit workspace / close settings |

## 🔒 Privacy

- **100% local by default** — nothing leaves your machine
- **No analytics** — zero tracking, zero telemetry
- **No accounts** — no login, no cloud sync
- **Your data stays yours** — config and playlists are plain JSON files on disk

**AI assistant caveat (honest disclosure):** the assistant is **optional and disabled** until you add your own Gemini key. When you *do* use it, your chat messages and library names (folder and clip titles) are sent to **Google Gemini** so it can interpret your request — that's the one thing that leaves your machine, and only while chatting. Video files themselves are never uploaded. Don't configure it (or leave it disabled) to keep the app fully offline.

## 🛡️ Security Notice

**This app is designed for local network use only.**

The built-in LAN guard rejects any request originating from a non-private IP — only loopback and RFC-1918 ranges (`10.x`, `172.16-31.x`, `192.168.x`) can reach the API. That keeps casual misconfiguration safe.

For your own safety, please:

- **Do NOT** port-forward port `7777` on your router
- **Do NOT** expose it via a public domain, reverse proxy, or VPN passthrough to untrusted networks
- **Do NOT** run this on a machine with a public IP

The app assumes every device on your LAN is trusted. If you share Wi-Fi with people you wouldn't hand the keys to, treat the API as openly accessible to them. Your router and firewall are the real perimeter — keep them that way.

Health probe: `GET /api/health` returns source availability + a UTC timestamp for uptime monitoring.

## 📝 License

**AGPL-3.0** — Free to use, modify, and self-host. If you deploy a modified version as a service, you must open-source your changes. You may not sell this software or offer it as a paid service. See [LICENSE](LICENSE).

---

Built with 🖤 by [@rikachu225](https://github.com/rikachu225)
