# Remote Desktop

[![CI](https://github.com/jamesabel/remotedesktop/actions/workflows/ci.yml/badge.svg)](https://github.com/jamesabel/remotedesktop/actions/workflows/ci.yml)
![Coverage](https://raw.githubusercontent.com/jamesabel/remotedesktop/badges/coverage.svg)
[![PyPI](https://img.shields.io/pypi/v/remotedesktop)](https://pypi.org/project/remotedesktop/)
![Python](https://img.shields.io/badge/python-3.14%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

A Python client/server application that provides remote desktop for Windows
computers on the same LAN, with autodiscovery of servers.

The server can optionally start automatically when you log in to Windows —
a checkbox on its Status tab registers it under the per-user Run key (no
administrator rights needed). The Status tab also has a *Restart server*
button that relaunches the app in a fresh process; since the server's
screen can be controlled remotely, this lets you update the software from
a remote desktop session and restart into the new version without visiting
the machine (approved clients reconnect without a new permission prompt).

Connections are made to the desktop screen, keyboard, mouse, and clipboard.
Other connections are not provided, such as shared drives, devices, or
multimedia (e.g., audio).

The screen transfer is optimized for ordinary desktop work — documents,
code, terminals — where most of the screen is static between frames: it is
lossless (pixel-exact at full resolution) and only changed screen regions
are transmitted. Smooth playback of fast-changing full-screen content, such
as video or games, is a non-goal.

The client and server are PySide6 (Qt) GUI apps, each run on its respective
computer. The client requests connection to the server, and for the initial
connection the user on the server side must permit the connection. After
that, the client can connect to the server whenever the server is running,
without the server having to grant permission again.

This does not use Windows RDP nor rely on any Microsoft-based authentication.

## Status

The core feature set works: autodiscovery, live screen viewing,
mouse/keyboard control, and two-way clipboard sync. A running server appears
in the client's *Servers* panel; opening it (after the server user approves
the first connection) shows the remote screen, clicking into the view
forwards mouse and keyboard input, and text/images copied on either side
appear on the other. Input forwarding is safe against interruptions: if the
viewer loses focus, a drag ends outside the view, or a client disconnects
mid-keystroke, anything still held down is released on the server — no stuck
keys or mouse buttons.

The connection is encrypted with TLS, and after the server user approves a
client once, that client reconnects automatically using a stored token.
Unapproved connections are limited to small handshake messages until the
server user admits them. The security model is tuned for a trusted LAN: the
server's certificate is self-signed and trusted on first use, favoring
reliable reconnection over strict certificate checking — if the server's
certificate ever changes, the client logs a warning and updates its stored
fingerprint rather than refusing to connect.

Both apps have a second tab listing every peer seen on the LAN — on the
server, the clients that have connected or attempted to; on the client, the
servers it has discovered or tried to reach — with each peer's current state,
number of attempts, and when it was first and last seen. From that tab the
server can revoke a client (disconnecting it and requiring approval to
reconnect) and the client can forget a server. This inventory, and all other
state (identity, pairings, settings), is stored in a SQLite database and
persists across restarts. Its location is chosen by `platformdirs` (on
Windows, `%LOCALAPPDATA%\remotedesktop`).

## Requirements

- Windows
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Running

From a clone of this repository, double-click `run_server.bat` on the
computer to be shared and `run_client.bat` on the viewing computer. Each
script prepares the environment (installing dependencies on first use) and
then launches the app, closing its console window once the app is running.

To run from a terminal instead:

```
uv run remotedesktop-server
uv run remotedesktop-client
```

The apps are also published on PyPI as
[`remotedesktop`](https://pypi.org/project/remotedesktop/).

## How discovery works

The client broadcasts a small JSON probe over UDP (port 48653); each server
on the LAN replies with its hostname and connection port. Windows Firewall
must allow Python to receive inbound UDP on that port for a server to be
discoverable from other machines.

## Development

```
uv sync          # set up the environment
uv run pytest    # run the tests
```

Run the tests from PowerShell or cmd, not Git Bash: Git Bash puts Git's
MinGW OpenSSL DLLs on `PATH`, which Qt's TLS backend loads and crashes on.
From PowerShell, Qt uses the Windows schannel backend as intended.

### The `badges` branch

The coverage badge above is served from the `badges` branch
(`raw.githubusercontent.com/.../badges/coverage.svg`). CI regenerates the
SVG after each test run on `master` and force-pushes it there as a single
orphan commit. It lives on its own branch because `master` only accepts
pull requests (a repository ruleset), so CI cannot commit to it directly;
keeping the badge in the repo avoids depending on an external coverage
service. The branch is generated output — never branch from it or merge it.
