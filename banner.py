"""
Aurora-cyberpunk RGB banner for the Video Collection launcher and server.

Two modes:
  - launch   Brief "Launching..." box, called by start scripts before server boots.
  - running  Full server info box with sources, paths, and copy-paste URLs.

CLI usage (from start.bat / start.sh):
  python banner.py launch
  python banner.py running 7777

Python usage (from server.py):
  from banner import print_running_banner
  print_running_banner(port=7777, sources=sources, site_name=site_name)

Design:
  - 24-bit ANSI RGB. Windows 10+, Windows Terminal, macOS, Linux all support it.
  - Gradient palette: hot pink -> magenta -> violet -> cyan -> mint-aqua.
    Cool-spectrum only (no rainbow). Saturated neons with aurora-like flow.
  - Borders carry the gradient, content stays high-contrast for readability.
  - Box width adapts to content. Paths are middle-truncated when too long.
  - LAN IP detected via OS routing table (UDP-connect trick, no actual packet).
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

# Force stdout to UTF-8 so Unicode box chars don't crash on Windows.
# Python defaults to cp1252 on Windows, which can't encode ╔═╗ etc.
# `chcp 65001` configures the terminal; this configures Python's encoder.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass

# ── ANSI primitives ────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"


def rgb(r: int, g: int, b: int) -> str:
    """24-bit foreground color escape."""
    return f"\033[38;2;{r};{g};{b}m"


# ── Aurora-cyberpunk gradient stops ────────────────────────────────
# Cool-spectrum only: pink -> magenta -> violet -> cyan -> mint-aqua.
GRADIENT_STOPS: list[tuple[int, int, int]] = [
    (255, 97, 200),   # hot pink
    (210, 95, 255),   # magenta
    (145, 115, 255),  # violet
    (90, 200, 255),   # electric cyan
    (110, 255, 210),  # mint-aqua
]

# ── Solid accent colors (used inside box rows) ─────────────────────
WHITE = rgb(245, 245, 250)
SOFT_WHITE = rgb(210, 215, 230)
PINK = rgb(255, 110, 200)
CYAN = rgb(120, 230, 255)
CYAN_BRIGHT = rgb(140, 255, 255)
MINT = rgb(120, 255, 200)
VIOLET = rgb(170, 140, 255)
GREEN = rgb(80, 255, 170)
RED = rgb(255, 80, 130)
YELLOW = rgb(255, 230, 120)
GRAY = rgb(120, 120, 145)

# ── Helpers ────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# Glyphs that render as 2 terminal columns. Kept empty by default because the
# URL rows now use only ASCII/Latin-1 chars (no ambiguous-width Unicode), so
# alignment is bulletproof without needing per-glyph width overrides.
#
# If you ever swap in a fancier decorative glyph and the right border drifts
# outward on that row, add the glyph here.
_WIDE_GLYPHS: frozenset[str] = frozenset()

# Zero-width formatting characters (variation selectors, ZWJ, ZWSP, etc.).
# Stripped from width calculations because they take no terminal columns.
# Using chr() for explicit, unambiguous code points (avoids editor encoding drift).
_ZERO_WIDTH = frozenset({
    chr(0xFE0E),  # variation selector-15 (text presentation)
    chr(0xFE0F),  # variation selector-16 (emoji presentation)
    chr(0x200B),  # zero-width space
    chr(0x200C),  # zero-width non-joiner
    chr(0x200D),  # zero-width joiner
})


def visible_len(s: str) -> int:
    """Terminal-column width of a string, accounting for wide glyphs and ANSI."""
    s = _ANSI_RE.sub("", s)
    width = 0
    for ch in s:
        if ch in _ZERO_WIDTH:
            continue
        width += 2 if ch in _WIDE_GLYPHS else 1
    return width


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def gradient_rgb(t: float) -> tuple[int, int, int]:
    """Sample the aurora gradient. t in [0, 1]."""
    t = max(0.0, min(1.0, t))
    if t == 0.0:
        return GRADIENT_STOPS[0]
    if t == 1.0:
        return GRADIENT_STOPS[-1]
    segments = len(GRADIENT_STOPS) - 1
    pos = t * segments
    idx = int(pos)
    frac = pos - idx
    a = GRADIENT_STOPS[idx]
    b = GRADIENT_STOPS[idx + 1]
    return (_lerp(a[0], b[0], frac), _lerp(a[1], b[1], frac), _lerp(a[2], b[2], frac))


def gradient_chars(char: str, width: int) -> str:
    """Repeat `char` `width` times with the aurora gradient across the run."""
    if width <= 0:
        return ""
    if width == 1:
        r, g, b = gradient_rgb(0.5)
        return f"{rgb(r, g, b)}{char}{RESET}"
    parts: list[str] = []
    for i in range(width):
        r, g, b = gradient_rgb(i / (width - 1))
        parts.append(f"{rgb(r, g, b)}{char}")
    parts.append(RESET)
    return "".join(parts)


def gradient_text(text: str) -> str:
    """Apply the aurora gradient across the visible characters of `text`."""
    if not text:
        return ""
    if len(text) == 1:
        r, g, b = gradient_rgb(0.5)
        return f"{rgb(r, g, b)}{text}{RESET}"
    parts: list[str] = []
    for i, ch in enumerate(text):
        r, g, b = gradient_rgb(i / (len(text) - 1))
        parts.append(f"{rgb(r, g, b)}{ch}")
    parts.append(RESET)
    return "".join(parts)


def truncate_path(path: str, max_len: int) -> str:
    """Shorten a path to fit `max_len` chars while keeping it readable.

    Strategy: keep drive/first segment + last two segments, with `…` in the middle.
    Falls back to a tail-only ellipsis if the path is still too long.
    """
    if len(path) <= max_len:
        return path
    parts = re.split(r"[\\/]", path)
    if len(parts) >= 3:
        head = parts[0]  # e.g. "D:"
        tail = "\\".join(parts[-2:])
        candidate = f"{head}\\…\\{tail}"
        if len(candidate) <= max_len:
            return candidate
    # Tail-only fallback
    return "…" + path[-(max_len - 1):]


def get_lan_ip() -> str:
    """Return the LAN IP via the OS routing table (no packet actually sent).

    Falls back to 127.0.0.1 when fully offline.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect does a route lookup but does not transmit.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _load_config(project_root: Path) -> dict:
    cfg_path = project_root / "data" / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ── Aurora strip — the one design primitive we need ────────────────
# Open design (no enclosed box) means we don't have to worry about
# right-edge alignment — content can be any width without breaking the look.
# Strips are HEAVY HORIZONTAL (━, U+2501) for a thicker "neon strip" feel
# compared to the lighter ═ used in the old closed-box design.
#
# At phase=0 the gradient is identical to the original static design
# (pink → magenta → violet → cyan → mint, sampled linearly). With phase > 0
# the gradient becomes cyclic and slides across the strip — same colors,
# just shifted, producing a Philips-Hue-style flow effect.
def aurora_strip(width: int, phase: float = 0.0) -> str:
    """Horizontal aurora-gradient strip with 2-space leading indent.

    Args:
        width: number of ━ chars in the strip.
        phase: animation offset in [0, 1). 0.0 = static look identical to the
               original design. Increasing phase rotates colors leftward.
    """
    if width <= 0:
        return "  "
    n_stops = len(GRADIENT_STOPS)
    # Viewport covers (n_stops-1)/n_stops of the cycle (4/5 with 5 stops),
    # so at phase=0 the strip shows exactly the linear pink → mint gradient
    # without the wraparound segment being visible.
    viewport = (n_stops - 1) / n_stops
    parts = ["  "]
    for i in range(width):
        t_norm = (i / (width - 1)) if width > 1 else 0.5
        u = (t_norm * viewport + phase) % 1.0
        pos = u * n_stops
        idx = int(pos) % n_stops
        frac = pos - int(pos)
        a = GRADIENT_STOPS[idx]
        b = GRADIENT_STOPS[(idx + 1) % n_stops]
        r = _lerp(a[0], b[0], frac)
        g = _lerp(a[1], b[1], frac)
        bl = _lerp(a[2], b[2], frac)
        parts.append(f"{rgb(r, g, bl)}━")
    parts.append(RESET)
    return "".join(parts)


# ── Background animation thread ────────────────────────────────────
def _start_strip_animation(strip_offsets: list[int], width: int) -> None:
    """Spawn a daemon thread that cycles the gradient on the given strip lines.

    Args:
        strip_offsets: line offsets ABOVE current cursor where each strip lives.
                       e.g. [12, 10, 5, 2] for the 4 strips of the running banner.
        width: strip width (matches the static render width).

    Behavior:
        - Daemon thread: dies automatically when the main thread exits (Ctrl+C).
        - Skips animation entirely if stdout is not a TTY (redirected to file).
        - Builds each frame as one string + single write+flush for atomicity.
        - Uses ANSI save/restore cursor so the user's prompt position is never
          disturbed — the cursor never visibly moves.
    """
    if not strip_offsets:
        return
    if not sys.stdout.isatty():
        return  # output is being piped/redirected — don't animate

    FPS = 15
    CYCLE_SECONDS = 8.0  # one full hue cycle — Philips-Hue ambient cadence
    PHASE_PER_FRAME = 1.0 / (FPS * CYCLE_SECONDS)
    FRAME_DELAY = 1.0 / FPS

    def loop() -> None:
        phase = 0.0
        while True:
            try:
                # Build one frame as a single string, then write atomically.
                buf = ["\033[s"]  # save initial cursor position once
                for offset in strip_offsets:
                    buf.append("\033[u")          # restore to initial each iter
                    buf.append(f"\033[{offset}F")  # cursor previous line N times
                    buf.append(aurora_strip(width, phase))
                buf.append("\033[u")  # final restore — cursor back where it was
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
                phase = (phase + PHASE_PER_FRAME) % 1.0
            except Exception:
                # Animation must never crash the server. If something goes
                # wrong (terminal closed, pipe broken, etc.) just stop quietly.
                return
            time.sleep(FRAME_DELAY)

    t = threading.Thread(target=loop, daemon=True, name="banner-aurora")
    t.start()


# ── Public API ─────────────────────────────────────────────────────
def print_launch_banner(project_root: Path | None = None) -> None:
    """Brief launch banner shown by start scripts before server boots."""
    project_root = project_root or Path(".")
    cfg = _load_config(project_root)
    site_name = cfg.get("siteName", "Media Center")

    title_line = (
        f"   {PINK}✦{RESET}  {gradient_text(site_name)}"
        f"{SOFT_WHITE} — Launching...{RESET}"
    )
    # Strip width: a bit wider than content so the strip "breathes".
    width = max(50, visible_len(title_line) + 6)

    print()
    print(aurora_strip(width))
    print(title_line)
    print(aurora_strip(width))
    print()


def print_running_banner(
    port: int,
    sources: Iterable[dict],
    site_name: str,
    project_root: Path | None = None,
) -> None:
    """Full server-running banner: title, source list, copy-paste URLs."""
    project_root = project_root or Path(".")
    sources = list(sources)

    lan_ip = get_lan_ip()
    show_lan = lan_ip != "127.0.0.1"

    localhost_url = f"http://localhost:{port}"
    lan_url = f"http://{lan_ip}:{port}"

    PATH_MAX = 56  # Truncate paths to at most this many chars.

    # ── Build content rows ─────────────────────────────────────────
    # Three sections (title / sources / URLs) separated by aurora strips.
    # No right border means we can use any Unicode glyph without alignment
    # math — icons are back: ♥ ● ⚡ ◆ ←
    title_text = f"{site_name} Media Center"
    title_row = f"   {PINK}♥{RESET}  {gradient_text(title_text)}"

    source_rows: list[str] = []
    if sources:
        for src in sources:
            name = src.get("name", "Unnamed")
            exists = Path(src["path"]).exists()
            dot = f"{GREEN}●{RESET}" if exists else f"{RED}●{RESET}"
            status_color = GREEN if exists else RED
            status = "ONLINE" if exists else "OFFLINE"
            source_rows.append(
                f"   {dot}  {WHITE}{name}{RESET} "
                f"{VIOLET}—{RESET} {status_color}{status}{RESET}"
            )
            truncated = truncate_path(src["path"], PATH_MAX)
            source_rows.append(f"      {GRAY}{truncated}{RESET}")
    else:
        source_rows.append(f"   {YELLOW}⚠{RESET}  {YELLOW}No media sources configured{RESET}")
        source_rows.append(f"      {GRAY}Add sources via Settings in the web UI{RESET}")

    url_rows: list[str] = [
        f"   {CYAN}⚡{RESET}  {CYAN}{localhost_url}{RESET}",
    ]
    if show_lan:
        # Bold + brighter cyan makes the LAN URL pop as the copy target.
        # Bold's width quirks don't matter here — no right border to drift.
        url_rows.append(
            f"   {MINT}◆{RESET}  {BOLD}{CYAN_BRIGHT}{lan_url}{RESET}"
            f"  {GRAY}← copy for LAN{RESET}"
        )

    # Strip width: match the widest content row + a little breathing room.
    # If visible_len is slightly off (Unicode width quirks), the strip is
    # just a bit shorter or longer — no alignment break, just aesthetics.
    all_rows = [title_row] + source_rows + url_rows
    width = max(50, max(visible_len(r) for r in all_rows) + 6)

    # ── Render ─────────────────────────────────────────────────────
    print()
    print(aurora_strip(width))
    print(title_row)
    print(aurora_strip(width))
    for row in source_rows:
        print(row)
    print(aurora_strip(width))
    for row in url_rows:
        print(row)
    print(aurora_strip(width))
    print()

    # ── Start Philips-Hue-style gradient animation ────────────────
    # Compute line offsets above current cursor for each of the 4 strips.
    # After the prints above, cursor sits 1 line below the trailing blank.
    # Going up from cursor:
    #   1 = trailing blank line
    #   2 = bottom strip
    #   3..2+n_url = URL rows (newest first as we count upward)
    #   2+n_url+1 = mid strip 2
    #   then n_source source rows
    #   then mid strip 1
    #   then title row
    #   then top strip
    n_url = len(url_rows)
    n_source = len(source_rows)
    bot_strip_off = 2
    mid_strip_2_off = bot_strip_off + n_url + 1
    mid_strip_1_off = mid_strip_2_off + n_source + 1
    top_strip_off = mid_strip_1_off + 2  # +1 for title row, +1 for the strip itself
    _start_strip_animation(
        [top_strip_off, mid_strip_1_off, mid_strip_2_off, bot_strip_off],
        width,
    )


# ── CLI entry point (for start scripts) ────────────────────────────
def _main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"launch", "running"}:
        print("usage: python banner.py {launch | running [port]}", file=sys.stderr)
        return 2

    # Enable ANSI on legacy Windows consoles if needed (no-op elsewhere).
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    project_root = Path(__file__).resolve().parent

    if argv[0] == "launch":
        print_launch_banner(project_root=project_root)
        return 0

    # running mode — load sources from config so the CLI matches the in-process call
    port = int(argv[1]) if len(argv) > 1 else 7777
    cfg = _load_config(project_root)
    print_running_banner(
        port=port,
        sources=cfg.get("mediaPaths", []),
        site_name=cfg.get("siteName", "Media Center"),
        project_root=project_root,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
