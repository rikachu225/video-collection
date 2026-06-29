@echo off
setlocal enabledelayedexpansion

:: UTF-8 codepage so Unicode box-drawing chars in banner.py render correctly.
chcp 65001 >nul

:: Navigate to script's own directory (critical for shell:startup compatibility).
cd /d "%~dp0"

:: Wait for venv to become available (drive may still be mounting at boot).
set "RETRIES=0"
set "MAX_RETRIES=15"
:wait_for_venv
if exist "venv\Scripts\activate.bat" goto venv_ready
set /a RETRIES+=1
if !RETRIES! gtr %MAX_RETRIES% (
    echo  [ERROR] Virtual environment not found after 30s. Run install.bat first.
    pause
    exit /b 1
)
echo  Waiting for drive to be ready... (!RETRIES!/%MAX_RETRIES%)
timeout /t 2 /nobreak >nul
goto wait_for_venv
:venv_ready

:: Activate venv.
call venv\Scripts\activate.bat

:: Set window title from config siteName so the taskbar matches the banner.
set "SITE_NAME=Media Center"
if exist "data\config.json" (
    for /f "usebackq delims=" %%a in (`python -c "import json; print(json.load(open('data/config.json')).get('siteName','Media Center'))"`) do set "SITE_NAME=%%a"
)
title !SITE_NAME!

:: Aurora-cyberpunk launch banner (Python handles width, RGB, Unicode).
python banner.py launch

:: Open browser after short delay so the server has time to bind.
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:7777"

:: Start server (server.py prints its own running banner with LAN IP).
python server.py 7777
