@echo off
setlocal enabledelayedexpansion
title Video Collection

:: Navigate to script's own directory (critical for shell:startup)
cd /d "%~dp0"

echo.
echo  ╔══════════════════════════════════════╗
echo  ║      Video Collection - Launch             ║
echo  ╚══════════════════════════════════════╝
echo.

:: Wait for venv to become available (drive may still be mounting at boot)
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

:: Activate and start
call venv\Scripts\activate.bat

:: Open browser after short delay
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:7777"

:: Start server
echo  Starting server on http://localhost:7777 ...
echo  Press Ctrl+C to stop.
echo.
python server.py 7777
