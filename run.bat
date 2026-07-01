@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
  python -m venv .venv
)

call .venv\Scripts\activate.bat
pip install -q -r requirements.txt

set PYTHONPATH=%CD%
set GOPRO_CLEANER_PORT=8765

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1

start http://127.0.0.1:8765/review
python -m gopro_cleaner
