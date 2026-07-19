@echo off
setlocal
cd /d "%~dp0.."

rem Build the signed Windows installer with pyship.
rem Requirements: the hardware signing token plugged in, a LOCAL console
rem session (pyship refuses to sign over RDP - failed PIN attempts can lock
rem the token), and optionally PYSHIP_SIGNING_CERTIFICATE_PIN set for
rem unattended signing (otherwise the token middleware prompts for the PIN).
rem CI builds the same installer unsigned; replace the release asset with
rem this signed one via:
rem   gh release upload v<version> installers\remotedesktop_installer_win64.exe --clobber

uv sync --group ship
if errorlevel 1 (
    echo Failed to create the environment.
    exit /b 1
)

rem pyship rebuilds dist\ (wheel), app\ (launcher + frozen CLIP), and installers\.
uv run --group ship python -m pyship --noupload --code-sign --certificate-auto-select
if errorlevel 1 (
    echo Installer build FAILED.
    exit /b 1
)

echo Installer: installers\remotedesktop_installer_win64.exe
