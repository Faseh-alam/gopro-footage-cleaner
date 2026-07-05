@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8765"
if defined GOPRO_CLEANER_PORT set "PORT=%GOPRO_CLEANER_PORT%"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

if not exist ".venv" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Failed to create venv. Install Python 3.10+ from https://python.org
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"
pip install -q -r requirements.txt
set "PYTHONPATH=%CD%"

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

echo Starting GoPro Footage Cleaner on port %PORT%...
start /MIN cmd /c "ping -n 4 127.0.0.1 >nul && start http://127.0.0.1:%PORT%/review"

%PY% -m gopro_cleaner
pause
