@echo off
setlocal
cd /d "%~dp0.."

rem Create/update .venv with the project and dev dependencies.
uv sync
if errorlevel 1 (
    echo Failed to create the environment.
    pause
    exit /b 1
)

echo .venv is ready.
