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

rem Only install deps if Flask is missing (pip every launch was hanging the UI open).
"%VENV_PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  "%VENV_PY%" -m pip install --upgrade pip
  "%VENV_PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    pause
    exit /b 1
  )
) else (
  echo Dependencies OK.
)

set "PYTHONPATH=%CD%"

echo Freeing port %PORT%...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
  echo Killing PID %%a on port %PORT%
  taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo Starting SD Card Offloader on port %PORT%...
start "SD Offloader" /MIN "%VENV_PY%" -m offloader

echo Waiting for server...
set /a TRIES=0
:waitloop
set /a TRIES+=1
if %TRIES% GTR 40 (
  echo.
  echo Server did not become ready on port %PORT%.
  echo Check the minimized "SD Offloader" window for errors.
  pause
  exit /b 1
)
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:%PORT%/api/health; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }"
if errorlevel 1 (
  ping -n 2 127.0.0.1 >nul
  goto waitloop
)

echo Server is up — opening browser.
start http://127.0.0.1:%PORT%/
echo.
echo Leave this window open. Press Ctrl+C or close the minimized server window to stop.
pause
