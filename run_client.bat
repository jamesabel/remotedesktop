@echo off
setlocal
cd /d "%~dp0"

rem Prepare the environment (installs/updates dependencies) before launching.
uv sync --quiet
if errorlevel 1 (
    echo Failed to prepare the environment.
    pause
    exit /b 1
)

rem Launch the GUI detached so this console window can close immediately.
start "" ".venv\Scripts\remotedesktop-client.exe" %*
