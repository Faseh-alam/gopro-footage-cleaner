@echo off
setlocal EnableExtensions
cd /d "%~dp0"

:: Development launcher: Vite hot-reload UI + Flask API (two processes).
:: For the production/colleague workflow use run.bat instead.

set "PORT=8765"
if defined GOPRO_CLEANER_PORT set "PORT=%GOPRO_CLEANER_PORT%"

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "FRONTEND_DIR=%~dp0gopro_cleaner\frontend"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "SYS_PY=py -3"
) else (
  set "SYS_PY=python"
)

if not exist "%FRONTEND_DIR%\package.json" (
  echo.
  echo ERROR: Frontend source not found at:
  echo   %FRONTEND_DIR%
  echo.
  echo For production ^(no Node^) use run.bat instead.
  pause
  exit /b 1
)

if not exist "%VENV_PY%" (
  echo Creating virtual environment...
  %SYS_PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo Failed to create venv. Install Python 3.10+ from https://python.org
    pause
    exit /b 1
  )
)

echo Installing Python dependencies...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies.
  pause
  exit /b 1
)

set "PYTHONPATH=%CD%"
set "GOPRO_LITE_MODE=1"

echo Ensuring FFmpeg...
"%VENV_PY%" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"
if errorlevel 1 (
  echo ERROR: Could not install FFmpeg.
  pause
  exit /b 1
)

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo Checking if Node.js is installed...
where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo Node.js is not found. Attempting to install via winget...
  winget install OpenJS.NodeJS -e --silent
  if errorlevel 1 (
    echo Failed to install Node.js automatically.
    echo Please install it from https://nodejs.org/ and re-run this script.
    pause
    exit /b 1
  )
  echo Node.js installed. Close this window and re-run run.dev.bat so PATH updates.
  pause
  exit /b 0
)

if not exist "%FRONTEND_DIR%\node_modules\" (
  echo Installing Vite dependencies ^(npm install^)...
  cd /d "%FRONTEND_DIR%"
  call npm install
  cd /d "%~dp0"
  if errorlevel 1 (
    echo Failed to install npm dependencies.
    pause
    exit /b 1
  )
) else (
  echo node_modules already exists. Skipping npm install...
)

echo.
echo Starting React ^(Vite^) frontend in a new window...
start "React Frontend" /D "%FRONTEND_DIR%" cmd /k "npm run dev"

echo Waiting for Vite to become ready ^(auto-detect port^)...
start /MIN "Open Vite Review" "%VENV_PY%" "%CD%\scripts\wait_open_vite.py"

echo Starting Flask API on port %PORT%...
"%VENV_PY%" -m gopro_cleaner

if errorlevel 1 (
  echo.
  echo Server exited with an error.
)
pause
