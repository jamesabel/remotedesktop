# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

A Python client/server remote desktop GUI application (PySide6) for Windows computers on the same LAN, per `readme.md`:

- **Autodiscovery and connection** of servers on the LAN.
- **In scope:** desktop screen, keyboard, mouse, and clipboard.
- **Out of scope:** shared drives, devices, and multimedia (e.g., audio).
- **Two GUI apps:** a client and a server, each run on its respective computer.
- **Trust model:** on first connection, the user on the server side must explicitly permit the client. After that, the client may reconnect whenever the server is running without further approval.
- **Constraint:** does not use Windows RDP and does not rely on any Microsoft-based authentication.

Both apps are currently window stubs with no networking implemented.

## Commands

Managed with `uv` (hatchling build backend, src layout):

- `uv sync` — create/update the venv with the project and dev dependencies
- `uv run pytest` — run all tests
- `uv run pytest tests/test_smoke.py::test_version` — run a single test
- `uv run remotedesktop-client` / `uv run remotedesktop-server` — launch the apps. These are `gui-scripts`, so they run detached with no console output; use `uv run python -m remotedesktop.client` (or `.server`) when you need stdout/tracebacks.
- `uv build` — build sdist and wheel into `dist/`
- `uv publish` — publish to PyPI

## Architecture

- Both apps are PySide6 GUI applications. `src/remotedesktop/client.py` (`ClientWindow`) and `src/remotedesktop/server.py` (`ServerWindow`) hold the app entry points; `main()` in each is wired to the `remotedesktop-client` / `remotedesktop-server` GUI scripts in `pyproject.toml`.
- The client-side remote desktop view is a widget, `ViewerWidget` in `src/remotedesktop/viewer.py`, hosted as `ClientWindow`'s central widget. Screen display and keyboard/mouse/clipboard forwarding belong in this widget, not in the window.
- The package version lives only in `src/remotedesktop/__init__.py` (`__version__`); hatchling reads it from there (`[tool.hatch.version]`), so bump it in that one place.
- Widget tests need a `QApplication`; use the session-scoped `qapp` fixture in `tests/conftest.py`.

## Environment Notes

- Target platform is Windows; development happens on Windows 11. Requires Python >=3.14.
