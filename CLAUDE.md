# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

A Python client/server remote desktop application for Windows computers on the same LAN, per `readme.md`:

- **Autodiscovery and connection** of servers on the LAN.
- **In scope:** desktop screen, keyboard, mouse, and clipboard.
- **Out of scope:** shared drives, devices, and multimedia (e.g., audio).
- **Two apps:** a client and a server, each run on its respective computer.
- **Trust model:** on first connection, the user on the server side must explicitly permit the client. After that, the client may reconnect whenever the server is running without further approval.
- **Constraint:** does not use Windows RDP and does not rely on any Microsoft-based authentication.

Both apps are currently unimplemented stubs.

## Commands

Managed with `uv` (hatchling build backend, src layout):

- `uv sync` — create/update the venv with the project and dev dependencies
- `uv run pytest` — run all tests
- `uv run pytest tests/test_smoke.py::test_version` — run a single test
- `uv run remotedesktop-client` / `uv run remotedesktop-server` — run the apps
- `uv build` — build sdist and wheel into `dist/`
- `uv publish` — publish to PyPI

## Structure

- `src/remotedesktop/client.py` and `src/remotedesktop/server.py` hold the two apps' entry points; `main()` in each is wired to the `remotedesktop-client` / `remotedesktop-server` console scripts in `pyproject.toml`.
- The package version lives only in `src/remotedesktop/__init__.py` (`__version__`); hatchling reads it from there (`[tool.hatch.version]`), so bump it in that one place.
- Tests live in `tests/`.

## Environment Notes

- Target platform is Windows; development happens on Windows 11. Requires Python >=3.11.
