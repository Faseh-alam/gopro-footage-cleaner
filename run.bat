@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8765"
if defined GOPRO_CLEANER_PORT set "PORT=%GOPRO_CLEANER_PORT%"

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "VENV_PIP=%CD%\.venv\Scripts\pip.exe"

rem Pick a system Python only for creating the venv (not for running the app).
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "SYS_PY=py -3"
) else (
  set "SYS_PY=python"
)

if not exist "%VENV_PY%" (
  echo Creating virtual environment...
  %SYS_PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo Failed to create venv. Install Python 3.10+ from https://python.org
    echo During install, check "Add python.exe to PATH".
    pause
    exit /b 1
  )
)

echo Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Failed to install dependencies. Check your internet connection and try again.
  pause
  exit /b 1
)

set "PYTHONPATH=%CD%"
set "GOPRO_LITE_MODE=1"
rem Lite mode auto-enables on PCs with 8GB RAM or less (fewer snapshots, less CPU/RAM).

echo Ensuring FFmpeg (uses system install, or downloads via static-ffmpeg)...
"%VENV_PY%" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"
if errorlevel 1 (
  echo.
  echo ERROR: Could not install FFmpeg. Check your internet connection and re-run run.bat.
  echo Or install manually: https://www.gyan.dev/ffmpeg/builds/
  echo.
  pause
  exit /b 1
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo Starting GoPro Footage Cleaner on port %PORT%...
start /MIN cmd /c "ping -n 4 127.0.0.1 >nul && start http://127.0.0.1:%PORT%/review"

"%VENV_PY%" -m gopro_cleaner
if errorlevel 1 (
  echo.
  echo Server exited with an error.
)
pause
