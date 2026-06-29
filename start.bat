@echo off
setlocal enabledelayedexpansion

:: Navigate to this script's own directory (portable + shell:startup compatible).
cd /d "%~dp0"

:: At boot the drive/filesystem may still be mounting; wait for the venv.
set "RETRIES=0"
set "MAX_RETRIES=90"

:wait_for_ready
if exist "venv\Scripts\activate.bat" goto ready
set /a RETRIES+=1
if !RETRIES! gtr %MAX_RETRIES% (
    echo  [ERROR] Could not find venv after 3 minutes. Run install.bat first.
    pause
    exit /b 1
)
echo  Waiting for system to be ready... (!RETRIES!/%MAX_RETRIES%)
timeout /t 2 /nobreak >nul
goto wait_for_ready

:ready
:: UTF-8 codepage so Unicode box-drawing chars in banner.py render correctly.
chcp 65001 >nul

call venv\Scripts\activate.bat

:: Read site name from config and use it for the window title.
set "SITE_NAME=Media Center"
if exist "data\config.json" (
    for /f "usebackq delims=" %%a in (`python -c "import json; print(json.load(open('data/config.json')).get('siteName','Media Center'))"`) do set "SITE_NAME=%%a"
)
title !SITE_NAME! Media Center

:: Aurora-cyberpunk launch banner (Python handles width, RGB, Unicode).
python banner.py launch

:: Start server (server.py prints its own running banner with LAN IP).
python server.py 7777
