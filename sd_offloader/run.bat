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

echo Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install dependencies.
  pause
  exit /b 1
)

set "PYTHONPATH=%CD%"

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo Starting SD Card Offloader on port %PORT%...
start /MIN cmd /c "ping -n 2 127.0.0.1 >nul && start http://127.0.0.1:%PORT%/"

"%VENV_PY%" -m offloader
if errorlevel 1 (
  echo Server exited with an error.
)
pause
