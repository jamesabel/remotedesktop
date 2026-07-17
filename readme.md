# Remote Desktop

A Python client/server application that provides remote desktop for Windows
computers on the same LAN, with autodiscovery of servers.

Connections are made to the desktop screen, keyboard, mouse, and clipboard.
Other connections are not provided, such as shared drives, devices, or
multimedia (e.g., audio).

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
appear on the other. The connection is currently unencrypted, so use it only
on a trusted LAN.

## Requirements

- Windows
- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Running

From a clone of this repository, double-click `run_server.bat` on the
computer to be shared and `run_client.bat` on the viewing computer, or run
from a terminal:

```
uv run remotedesktop-server
uv run remotedesktop-client
```

`uv run` creates the virtual environment and installs dependencies
automatically on first use.

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
