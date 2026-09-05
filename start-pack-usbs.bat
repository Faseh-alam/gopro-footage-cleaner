@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONPATH=%CD%"
if exist "%CD%\.venv\Scripts\python.exe" (
  set "PY=%CD%\.venv\Scripts\python.exe"
) else (
  set "PY=python"
)
echo Voiceover USB packer
echo   plan / show / fill
echo.
"%PY%" -m pack_voiceover_usbs %*
exit /b %ERRORLEVEL%
