"""
Desktop launcher for Video Collection.
Opens a native window using pywebview with Edge WebView2.
Flask/waitress runs in a background thread.
"""

import json
import sys
import threading
import time
from pathlib import Path


def main():
    import webview

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7777

    # Read config for window title
    config_path = Path(__file__).parent / "data" / "config.json"
    site_name = "My Collection"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            site_name = config.get("siteName", site_name)
        except Exception:
            pass

    # Start Flask/waitress in a daemon thread
    def run_server():
        from server import app
        from waitress import serve
        serve(app, host="127.0.0.1", port=port, threads=8)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(0.8)

    # Create native window
    window = webview.create_window(
        site_name,
        f"http://127.0.0.1:{port}",
        width=1920,
        height=1080,
        min_size=(800, 600),
        text_select=False,
    )

    webview.start()


if __name__ == "__main__":
    main()
