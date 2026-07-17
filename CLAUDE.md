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

LAN autodiscovery and screen sharing (with the first-connection approval flow) are implemented; keyboard/mouse/clipboard forwarding is not yet.

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
- **Autodiscovery** (`src/remotedesktop/discovery.py`) is a stdlib-only UDP probe/response protocol, deliberately not mDNS: the client broadcasts a JSON probe to `DISCOVERY_PORT` (48653) and servers reply with `{name, port}`; datagrams with the wrong magic/version/type are dropped. The server runs a `DiscoveryResponder` thread while its window is open; the client's `DiscoveryPanel` (dock in `ClientWindow`) calls the blocking `discover_servers()` on a worker thread and delivers results to the GUI via a queued signal. `DEFAULT_CONNECT_PORT` (48654) is reserved for the future desktop connection.
- Discovery tests run over loopback with ephemeral ports (`bind_host`/`discovery_port`/`broadcast_hosts` parameters exist for this), so they never touch the real LAN or fixed ports.
- **Screen sharing** (`sharing.py` + `protocol.py`) runs entirely on the Qt event loop — no threads or locks. `MessageStream` frames messages over a QTcpSocket (4-byte length + kind byte; JSON control messages or JPEG frames; malformed input aborts the socket). `ShareServer` owns one QTimer that grabs the primary screen via `QScreen.grabWindow(0)`, JPEG-encodes once, and fans out to all clients, skipping clients whose send buffer is backlogged; the timer only runs while clients are connected. `ShareClient` decodes frames to QImage for the viewer.
- **Trust model plumbing** (`config.py`): the client has a stable UUID identity and the server a persisted approved-ID set, both under `%APPDATA%/remotedesktop/`. `ShareServer` takes an `approve_client(client_id, name) -> bool` callback; `ServerWindow` implements it as a modal QMessageBox. Tests inject `ApprovedClients(tmp_path/...)` and explicit identities so they never touch real APPDATA or prompt.
- **Status/debug logging is a feature**: `ShareServer`, `ShareClient`, and `DiscoveryPanel` emit human-readable `status` signals for every connection phase, and both windows show them in a timestamped "Connection log" pane. When adding connection behavior, emit a status message for each new phase or failure path — there's a test asserting the server's phase messages.
- Sharing tests drive real sockets on the GUI thread by pumping `qapp.processEvents()` until a condition holds (see `pump()` in `tests/test_sharing.py`).
- The package version lives only in `src/remotedesktop/__init__.py` (`__version__`); hatchling reads it from there (`[tool.hatch.version]`), so bump it in that one place.
- Widget tests need a `QApplication`; use the session-scoped `qapp` fixture in `tests/conftest.py`.

## Environment Notes

- Target platform is Windows; development happens on Windows 11. Requires Python >=3.14.
