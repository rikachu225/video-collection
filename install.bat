@echo off
setlocal enabledelayedexpansion
title Media Center - Install

:: Navigate to script's own directory
cd /d "%~dp0"

echo.
echo  +========================================+
echo  ^|       Media Center - Install           ^|
echo  +========================================+
echo.

:: Check Python -- install via winget if missing
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo  Python not found. Installing via winget...
    where winget >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo  [ERROR] Neither Python nor winget found.
        echo          Install Python 3.10+ from https://python.org/downloads
        pause
        exit /b 1
    )
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo  ================================================
    echo   Python was installed. Close this terminal,
    echo   open a NEW terminal, and run install.bat again
    echo   so Python is on your PATH.
    echo  ================================================
    echo.
    pause
    exit /b 0
)

:: Create venv
if not exist "venv" (
    echo  Creating virtual environment...
    python -m venv venv
    if !ERRORLEVEL! neq 0 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  Virtual environment created.
) else (
    echo  Virtual environment already exists.
)

:: Install dependencies
echo  Installing dependencies...
call venv\Scripts\activate.bat
pip install -r requirements.txt --quiet
if %ERRORLEVEL% neq 0 (
    echo  [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

:: Clean data directory for fresh start
if exist "data" (
    echo  Cleaning data folder for fresh setup...
    del /q "data\config.json" 2>nul
    del /q "data\theater.json" 2>nul
    del /q "data\playlists.json" 2>nul
) else (
    mkdir data
)

:: Personalization prompts
echo.
echo  ----------------------------------------
echo   Let's personalize your media center!
echo  ----------------------------------------
echo.

set "SITE_NAME="
set /p "SITE_NAME=  What do you want to call your site? : "
if "!SITE_NAME!"=="" set "SITE_NAME=My Collection"

set "THEATER_NAME="
set /p "THEATER_NAME=  What do you want to call your theater? : "
if "!THEATER_NAME!"=="" set "THEATER_NAME=My Theater"

echo  Creating config with your custom names...
python -c "import json,sys; json.dump({'siteName': sys.argv[1], 'theaterName': sys.argv[2], 'mediaPaths': [], 'excludedFolders': ['Scripts', 'scripts']}, open('data/config.json', 'w'), indent=2)" "!SITE_NAME!" "!THEATER_NAME!"
echo  Config saved!

echo.
echo  ========================================
echo   Install complete!
echo   Run 'start.bat' to launch in browser.
echo   Run 'start_desktop.bat' for native window.
echo  ========================================
echo.
pause
