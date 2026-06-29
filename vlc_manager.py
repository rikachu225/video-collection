"""
VLC Workspace Manager
Creates borderless overlay windows for VLC video playback,
positioned over pywebview workspace panels.
"""

import ctypes
import ctypes.wintypes
import threading
import time
import sys

# Win32 constants
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_CLIPCHILDREN = 0x02000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
SWP_NOACTIVATE = 0x0010
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = ctypes.wintypes.HWND(-1)

user32 = ctypes.windll.user32 if sys.platform == "win32" else None

# ── Declare Win32 function signatures ─────────────────────────
# Without explicit argtypes, ctypes defaults to signed int which
# overflows on WS_POPUP (0x80000000) and similar large flags.
if user32:
    HWND = ctypes.wintypes.HWND
    HINSTANCE = ctypes.wintypes.HINSTANCE
    HMENU = ctypes.wintypes.HMENU
    DWORD = ctypes.wintypes.DWORD
    BOOL = ctypes.wintypes.BOOL
    UINT = ctypes.wintypes.UINT
    INT = ctypes.c_int
    LPVOID = ctypes.c_void_p
    LPCWSTR = ctypes.c_wchar_p
    ATOM = ctypes.wintypes.ATOM

    user32.CreateWindowExW.argtypes = [
        DWORD, LPCWSTR, LPCWSTR, DWORD,
        INT, INT, INT, INT,
        HWND, HMENU, HINSTANCE, LPVOID,
    ]
    user32.CreateWindowExW.restype = HWND

    user32.SetWindowPos.argtypes = [
        HWND, HWND, INT, INT, INT, INT, UINT,
    ]
    user32.SetWindowPos.restype = BOOL

    user32.ShowWindow.argtypes = [HWND, INT]
    user32.ShowWindow.restype = BOOL

    user32.DestroyWindow.argtypes = [HWND]
    user32.DestroyWindow.restype = BOOL

    user32.RegisterClassExW.restype = ATOM

    user32.DefWindowProcW.argtypes = [HWND, UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.wintypes.LPARAM

# ── Custom window class for VLC rendering ─────────────────────
# "Static" window class can't handle DirectX rendering surfaces.
# We register a custom class with DefWindowProcW so VLC's Direct3D
# output has a proper window to render into.
_wnd_class_registered = False
_wnd_class_name = "VLCOverlay"
_def_wnd_proc = None  # prevent garbage collection

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.wintypes.LPARAM, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
)


def _ensure_wnd_class():
    """Register the VLCOverlay window class (once)."""
    global _wnd_class_registered, _def_wnd_proc
    if _wnd_class_registered or not user32:
        return

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p),
            ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
            ("hIconSm", ctypes.c_void_p),
        ]

    # Wrap DefWindowProcW as a Python callback for the window class.
    # This is the standard pattern: a thin Python wrapper that delegates
    # to the real DefWindowProcW, stored as a WNDPROC callback.
    def _py_def_wnd_proc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _def_wnd_proc = WNDPROC(_py_def_wnd_proc)

    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = _def_wnd_proc
    wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
    wc.hbrBackground = ctypes.c_void_p(0)
    wc.lpszClassName = _wnd_class_name

    user32.RegisterClassExW(ctypes.byref(wc))
    _wnd_class_registered = True


class VLCPanel:
    """Manages a single VLC player instance + native overlay window."""

    def __init__(self, vlc_instance, parent_hwnd, file_path, index):
        self.index = index
        self.file_path = str(file_path)
        self.parent_hwnd = parent_hwnd
        self.vlc_instance = vlc_instance
        self.player = vlc_instance.media_player_new()
        self.hwnd = None
        self.playing = False
        self.muted = True
        self.speed = 1.0
        self.volume = 100
        self.loop_start = None
        self.loop_end = None
        self._loop_active = False

    def create_window(self, x, y, w, h):
        """Create a borderless popup window owned by parent for VLC rendering."""
        if not user32:
            return

        _ensure_wnd_class()

        # argtypes on CreateWindowExW handle DWORD conversion;
        # we just need to keep values within unsigned 32-bit range.
        ex_style = (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST) & 0xFFFFFFFF
        style = (WS_POPUP | WS_VISIBLE | WS_CLIPCHILDREN) & 0xFFFFFFFF

        self.hwnd = user32.CreateWindowExW(
            ex_style, _wnd_class_name, None, style,
            int(x), int(y), int(w), int(h),
            self.parent_hwnd or 0, None, None, None,
        )

        # set_hwnd BEFORE set_media — VLC needs the render target early
        self.player.set_hwnd(self.hwnd)

        # Load media
        media = self.vlc_instance.media_new(self.file_path)
        media.add_option("input-repeat=65535")
        self.player.set_media(media)

    def move(self, x, y, w, h):
        """Reposition and resize the VLC overlay window (always on top)."""
        if self.hwnd and user32:
            user32.SetWindowPos(
                self.hwnd, HWND_TOPMOST,
                int(x), int(y), int(w), int(h),
                SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )

    def show(self):
        """Show the overlay window."""
        if self.hwnd and user32:
            user32.ShowWindow(self.hwnd, 5)  # SW_SHOW

    def hide(self):
        """Hide the overlay window."""
        if self.hwnd and user32:
            user32.ShowWindow(self.hwnd, 0)  # SW_HIDE

    def play(self):
        """Start or resume playback."""
        self.player.play()
        self.playing = True
        # Start A-B loop polling if loop points are set
        if self.loop_start is not None and self.loop_end is not None:
            self._start_loop_polling()

    def pause(self):
        """Pause playback."""
        self.player.pause()
        self.playing = False

    def stop(self):
        """Stop playback."""
        self._loop_active = False
        self.player.stop()
        self.playing = False

    def set_speed(self, rate):
        """Set playback speed (0.25 - 4.0)."""
        self.speed = float(rate)
        self.player.set_rate(self.speed)

    def set_volume(self, vol):
        """Set volume 0-200 (VLC natively supports >100% boost)."""
        self.volume = int(vol)
        self.player.audio_set_volume(self.volume)

    def toggle_mute(self):
        """Toggle mute. Returns new muted state."""
        self.muted = not self.muted
        self.player.audio_set_volume(0 if self.muted else self.volume)
        return self.muted

    def set_loop(self, start_sec, end_sec):
        """Set custom A-B loop points (in seconds). None to clear."""
        if start_sec is not None and end_sec is not None:
            self.loop_start = float(start_sec)
            self.loop_end = float(end_sec)
            if self.playing:
                self._start_loop_polling()
        else:
            self.loop_start = None
            self.loop_end = None
            self._loop_active = False

    def _start_loop_polling(self):
        """Poll VLC position for A-B loop enforcement."""
        if self._loop_active:
            return  # Already polling
        self._loop_active = True

        def poll():
            while self._loop_active and self.loop_end is not None:
                try:
                    pos_ms = self.player.get_time()
                    if pos_ms >= 0 and pos_ms >= self.loop_end * 1000:
                        self.player.set_time(int(self.loop_start * 1000))
                except Exception:
                    pass
                time.sleep(0.05)

        t = threading.Thread(target=poll, daemon=True)
        t.start()

    def destroy(self):
        """Release VLC player and destroy the native window."""
        self._loop_active = False
        try:
            self.player.stop()
        except Exception:
            pass
        try:
            self.player.release()
        except Exception:
            pass
        if self.hwnd and user32:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


class VLCWorkspaceManager:
    """Manages all VLC panels for workspace mode."""

    def __init__(self):
        self._vlc = None
        self._panels = {}  # index (int) -> VLCPanel
        self._parent_hwnd = None
        self._available = None

    def check_vlc(self):
        """Check if VLC (libvlc) is available on the system."""
        if self._available is not None:
            return self._available
        try:
            import vlc
            inst = vlc.Instance("--quiet")
            inst.release()
            self._available = True
        except Exception:
            self._available = False
        return self._available

    def init(self, parent_hwnd):
        """Initialize VLC instance with parent window handle."""
        if self._vlc:
            return  # Already initialized
        import vlc
        self._vlc = vlc.Instance("--quiet", "--no-video-title-show", "--vout=direct3d9")
        self._parent_hwnd = parent_hwnd

    def open_panel(self, index, file_path, x, y, w, h,
                   loop_start=None, loop_end=None):
        """Create a VLC panel at the given screen coordinates."""
        index = int(index)
        # Close existing panel at this index if any
        if index in self._panels:
            self._panels[index].destroy()

        panel = VLCPanel(self._vlc, self._parent_hwnd, file_path, index)
        panel.create_window(x, y, w, h)

        # Set loop points if provided
        if loop_start is not None and loop_end is not None:
            panel.set_loop(loop_start, loop_end)

        self._panels[index] = panel
        return True

    def play_panel(self, index):
        index = int(index)
        if index in self._panels:
            p = self._panels[index]
            p.play()
            # Start muted (workspace default)
            if p.muted:
                p.player.audio_set_volume(0)

    def pause_panel(self, index):
        index = int(index)
        if index in self._panels:
            self._panels[index].pause()

    def move_panel(self, index, x, y, w, h):
        index = int(index)
        if index in self._panels:
            self._panels[index].move(x, y, w, h)

    def set_speed(self, index, rate):
        index = int(index)
        if index in self._panels:
            self._panels[index].set_speed(rate)

    def set_volume(self, index, vol):
        index = int(index)
        if index in self._panels:
            self._panels[index].set_volume(vol)

    def toggle_mute(self, index):
        index = int(index)
        if index in self._panels:
            return self._panels[index].toggle_mute()
        return True

    def set_loop(self, index, start_sec, end_sec):
        index = int(index)
        if index in self._panels:
            self._panels[index].set_loop(start_sec, end_sec)

    def remove_panel(self, index):
        index = int(index)
        if index in self._panels:
            self._panels[index].destroy()
            del self._panels[index]

    def close_all(self):
        """Destroy all VLC panels and overlay windows."""
        for panel in list(self._panels.values()):
            panel.destroy()
        self._panels.clear()

    def play_all(self):
        for p in self._panels.values():
            p.play()
            if p.muted:
                p.player.audio_set_volume(0)

    def pause_all(self):
        for p in self._panels.values():
            p.pause()

    def mute_all(self):
        for p in self._panels.values():
            p.muted = True
            p.player.audio_set_volume(0)

    def unmute_all(self):
        for p in self._panels.values():
            p.muted = False
            p.player.audio_set_volume(p.volume)

    def set_volume_all(self, vol):
        vol = int(vol)
        for p in self._panels.values():
            p.volume = vol
            if not p.muted:
                p.player.audio_set_volume(vol)

    def release(self):
        """Full cleanup: close all panels and release VLC instance."""
        self.close_all()
        if self._vlc:
            try:
                self._vlc.release()
            except Exception:
                pass
            self._vlc = None
