@echo off
setlocal EnableExtensions
cd /d "%~dp0"

:: One-click Voiceover Station for narrators (Windows).
:: Double-click this file. No coding required.
:: Opens http://127.0.0.1:8765/voiceover

set "GOPRO_START_PATH=/voiceover"
set "GOPRO_APP_NAME=Voiceover Station"
call "%~dp0run.bat"
exit /b %ERRORLEVEL%
