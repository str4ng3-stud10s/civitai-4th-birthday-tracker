@echo off
setlocal
cd /d "%~dp0"
mode con: cols=140 lines=100 >nul 2>&1 || echo Note: this terminal could not be resized automatically.

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0civitai4bday.py" %*
) else (
    python "%~dp0civitai4bday.py" %*
)

if errorlevel 1 pause
