#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo ""
echo "  +========================================+"
echo "  |       Media Center - Install           |"
echo "  +========================================+"
echo ""

# Fix file ownership (Windows -> Mac transfers cause root ownership)
CURRENT_USER=$(whoami)
PROJECT_DIR="$(pwd)"

check_ownership() {
    # Check if any files in the project are NOT owned by current user
    if [ "$(uname)" = "Darwin" ] || [ "$(uname)" = "Linux" ]; then
        BAD_FILES=$(find "$PROJECT_DIR" -maxdepth 2 -not -user "$CURRENT_USER" 2>/dev/null | head -5)
        if [ -n "$BAD_FILES" ]; then
            return 0  # found files with wrong ownership
        fi
    fi
    return 1  # all good
}

if check_ownership; then
    echo "  Some files are not owned by '$CURRENT_USER'."
    echo "  This happens when transferring from Windows."
    echo "  We need your password to fix file permissions."
    echo ""
    sudo chown -R "$CURRENT_USER" "$PROJECT_DIR"
    echo "  Ownership fixed!"
    echo ""
fi

# Check Python
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo "  [ERROR] Python not found. Install Python 3.10+ first."
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)

# Remove broken venv from another OS if detected
if [ -d "venv" ] && [ ! -f "venv/bin/activate" ] && [ ! -f "venv/Scripts/activate" ]; then
    echo "  Removing incompatible virtual environment..."
    rm -rf venv
fi

# Create venv
if [ ! -d "venv" ]; then
    echo "  Creating virtual environment..."
    $PYTHON -m venv venv
    echo "  Virtual environment created."
else
    echo "  Virtual environment already exists."
fi

# Install deps
echo "  Installing dependencies..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
elif [ -f "venv/Scripts/activate" ]; then
    source venv/Scripts/activate
fi
pip install -r requirements.txt --quiet

# Clean data directory for fresh start
if [ -d "data" ]; then
    echo "  Cleaning data folder for fresh setup..."
    rm -f data/config.json data/theater.json data/playlists.json
else
    mkdir -p data
fi

# Personalization prompts
echo ""
echo "  ----------------------------------------"
echo "   Let's personalize your media center!"
echo "  ----------------------------------------"
echo ""

read -rp "  What do you want to call your site? : " SITE_NAME
SITE_NAME="${SITE_NAME:-My Collection}"

read -rp "  What do you want to call your theater? : " THEATER_NAME
THEATER_NAME="${THEATER_NAME:-My Theater}"

echo "  Creating config with your custom names..."
$PYTHON -c "
import json, sys
json.dump({
    'siteName': sys.argv[1],
    'theaterName': sys.argv[2],
    'mediaPaths': [],
    'excludedFolders': ['Scripts', 'scripts']
}, open('data/config.json', 'w'), indent=2)
" "$SITE_NAME" "$THEATER_NAME"
echo "  Config saved!"

echo ""
echo "  ========================================"
echo "   Install complete!"
echo "   Run './start.sh' to launch the server."
echo "  ========================================"
echo ""
