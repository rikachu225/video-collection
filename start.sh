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

# Read site name from config for display
SITE_NAME=$(python -c "import json; print(json.load(open('data/config.json')).get('siteName','Media Center'))" 2>/dev/null || echo "Media Center")

echo ""
echo "  +========================================+"
echo "  |   $SITE_NAME - Launch"
echo "  +========================================+"
echo ""

# Open browser after delay
(sleep 2 && python -m webbrowser "http://localhost:$PORT") &

echo "  Starting server on http://localhost:$PORT ..."
echo "  Press Ctrl+C to stop."
echo ""
python server.py $PORT
