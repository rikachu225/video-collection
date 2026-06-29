#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Activate venv
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
else
    echo "  [ERROR] Virtual environment not found. Run install.sh first."
    exit 1
fi

PORT=${1:-7777}

# Aurora-cyberpunk launch banner (Python handles width, RGB, Unicode).
python banner.py launch

# Open browser after delay so the server has time to bind.
(sleep 2 && python -m webbrowser "http://localhost:$PORT") &

# Start server (server.py prints its own running banner with LAN IP).
python server.py $PORT
