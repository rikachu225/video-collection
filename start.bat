@echo off
setlocal enabledelayedexpansion

:: At boot, the drive/filesystem may not be ready yet.
:: Hardcode the full path so nothing depends on %~dp0 resolution timing.
set "PROJECT=G:\Gemini CLI\Video Collection"
set "RETRIES=0"
set "MAX_RETRIES=90"

:wait_for_ready
if exist "%PROJECT%\venv\Scripts\activate.bat" goto ready
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
cd /d "%PROJECT%"
call venv\Scripts\activate.bat

:: Read site name from config for display
set "SITE_NAME=Media Center"
if exist "data\config.json" (
    for /f "usebackq delims=" %%a in (`python -c "import json; print(json.load(open('data/config.json')).get('siteName','Media Center'))"`) do set "SITE_NAME=%%a"
)

title !SITE_NAME!

echo.
echo  +========================================+
echo  ^|   !SITE_NAME! - Launch
echo  +========================================+
echo.

:: Start server
echo  Starting server on http://localhost:7777 ...
echo  Press Ctrl+C to stop.
echo.
python server.py 7777
