@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8877"
if defined SD_OFFLOADER_PORT set "PORT=%SD_OFFLOADER_PORT%"

set "VENV_PY=%CD%\.venv\Scripts\python.exe"

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
    echo Failed to create venv. Install Python 3.10+ from https://python.org
    pause
    exit /b 1
  )
)

"%VENV_PY%" -c "import flask,waitress" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  "%VENV_PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
)

set "PYTHONPATH=%CD%"
set "SD_OFFLOADER_OPEN_BROWSER=1"

echo.
echo === SD Card Offloader ===
echo Folder: %CD%
echo Port:   %PORT%
echo.

echo Freeing port %PORT% if needed...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
  echo   killing PID %%a
  taskkill /PID %%a /F >nul 2>&1
)

echo.
echo Starting server in THIS window...
echo When you see "Ready — opening browser", the UI should open.
echo Press Ctrl+C to stop.
echo.

"%VENV_PY%" -m offloader
echo.
echo Server stopped.
pause
