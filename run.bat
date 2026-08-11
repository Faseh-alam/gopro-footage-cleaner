@echo off
setlocal EnableExtensions
cd /d "%~dp0"

:: Production launcher: Flask serves API + built UI from gopro_cleaner\web
:: (no Vite / Node required). For local UI development use run.dev.bat.

set "PORT=8765"
if defined GOPRO_CLEANER_PORT set "PORT=%GOPRO_CLEANER_PORT%"

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "WEB_DIR=%CD%\gopro_cleaner\web"
set "FRONTEND_DIR=%CD%\gopro_cleaner\frontend"
set "APP_URL=http://127.0.0.1:%PORT%/review"

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

:: Prefer committed build; optionally rebuild if frontend source + Node are present.
if exist "%WEB_DIR%\index.html" goto :have_web
if exist "%WEB_DIR%\_shell.html" goto :have_web

if not exist "%FRONTEND_DIR%\package.json" (
  echo.
  echo ERROR: No UI build found at gopro_cleaner\web
  echo Ask a teammate for the built web folder, or restore frontend source and run:
  echo   cd gopro_cleaner\frontend
  echo   npm install
  echo   npm run build:flask
  pause
  exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: UI build missing and Node.js is not installed to build it.
  echo Install Node.js from https://nodejs.org/ or copy gopro_cleaner\web from the repo.
  pause
  exit /b 1
)

echo Building UI for Flask ^(npm run build:flask^)...
cd /d "%FRONTEND_DIR%"
if not exist "node_modules\" call npm install
if errorlevel 1 (
  echo Failed npm install.
  cd /d "%~dp0"
  pause
  exit /b 1
)
call npm run build:flask
if errorlevel 1 (
  echo Failed to build UI.
  cd /d "%~dp0"
  pause
  exit /b 1
)
cd /d "%~dp0"

:have_web
echo Using UI from gopro_cleaner\web

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo.
if defined GOPRO_NO_BROWSER (
  echo Skipping browser launch — the open tab reloads itself after an update.
) else (
  echo Opening %APP_URL%
  start "" "%APP_URL%"
)

echo Starting Flask ^(API + UI^) on port %PORT%...
"%VENV_PY%" -m gopro_cleaner

if errorlevel 1 (
  echo.
  echo Server exited with an error.
)
pause
exit /b %ERRORLEVEL%
