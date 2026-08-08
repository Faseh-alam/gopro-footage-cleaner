@echo off
setlocal EnableExtensions
cd /d "%~dp0"

:: ==========================================
:: 1. FLASK BACKEND SETUP
:: ==========================================
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

echo Installing Python dependencies...
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

echo Ensuring FFmpeg (uses system install, or downloads via static-ffmpeg)...
"%VENV_PY%" -c "from gopro_cleaner.core.ffmpeg_tools import ensure_ffmpeg; s=ensure_ffmpeg(); raise SystemExit(0 if s.get('ok') else 1)"
if errorlevel 1 (
  echo.
  echo ERROR: Could not install FFmpeg. Check your internet connection and re-run run.bat.
  pause
  exit /b 1
)

:: Kill any existing process on the Flask port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1


:: ==========================================
:: 2. NODE.JS & VITE FRONTEND SETUP
:: ==========================================
echo Checking if Node.js is installed...
where node >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo Node.js is not found. Attempting to install via winget...
    winget install OpenJS.NodeJS -e --silent
    if errorlevel 1 (
        echo Failed to install Node.js automatically.
        echo Please manually install it from https://nodejs.org/ and re-run this script.
        pause
        exit /b 1
    )
    echo.
    echo Node.js installed successfully! 
    echo IMPORTANT: You must close this command window and re-run run.bat so the system recognizes the 'node' command.
    pause
    exit /b 0
)

:: Absolute path to your Vite frontend subfolder
set "FRONTEND_DIR=%~dp0gopro_cleaner\frontend"

if not exist "%FRONTEND_DIR%" (
    echo.
    echo ERROR: Could not find frontend folder at: %FRONTEND_DIR%
    pause
    exit /b 1
)

:: Check if node_modules exists inside gopro_cleaner\frontend
if not exist "%FRONTEND_DIR%\node_modules\" (
    echo.
    echo Installing Vite dependencies ^(npm install^)...
    cd /d "%FRONTEND_DIR%"
    call npm install
    cd /d "%~dp0"
    if errorlevel 1 (
        echo Failed to install npm dependencies. Check your internet connection.
        pause
        exit /b 1
    )
) else (
    echo node_modules already exists. Skipping npm install...
)


:: ==========================================
:: 3. STARTING BOTH SERVERS & BROWSER
:: ==========================================
echo.
echo Starting React (Vite) Frontend in a new window...
:: Spawns Vite in a separate, persistent window
start "React Frontend" /D "%FRONTEND_DIR%" cmd /k "npm run dev"

echo Waiting for Vite to become ready (auto-detect port)...
start /MIN "Open Vite Review" "%VENV_PY%" "%CD%\scripts\wait_open_vite.py"

echo Starting Flask Backend on port %PORT%...
:: Runs Flask in the MAIN window (just like your original script)
"%VENV_PY%" -m gopro_cleaner

if errorlevel 1 (
    echo.
    echo Server exited with an error.
)
pause