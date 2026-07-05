@echo off
rem ---------------------------------------------------------------------
rem Modality Emulator launcher (double-click to run)
rem
rem First run: creates a local .venv and installs the dependencies.
rem Every run: starts the web app, then opens the browser at the UI.
rem Close this window (or Ctrl+C) to stop the emulator.
rem ---------------------------------------------------------------------
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher 'py' not found. Install Python 3 from python.org
    echo and check "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv || (pause & exit /b 1)
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (pause & exit /b 1)
)

echo Starting Modality Emulator on http://127.0.0.1:8080 ...
start "" http://127.0.0.1:8080
".venv\Scripts\python.exe" main.py
pause
