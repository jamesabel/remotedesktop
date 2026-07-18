# Remote Desktop

[![CI](https://github.com/jamesabel/remotedesktop/actions/workflows/ci.yml/badge.svg)](https://github.com/jamesabel/remotedesktop/actions/workflows/ci.yml)
![Coverage](https://raw.githubusercontent.com/jamesabel/remotedesktop/badges/coverage.svg)
[![PyPI](https://img.shields.io/pypi/v/remotedesktop)](https://pypi.org/project/remotedesktop/)
![Python](https://img.shields.io/badge/python-3.14%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**Lossless, low-latency remote desktop for Windows computers on your LAN — pure Python, zero configuration.**

Run the server on the computer you want to reach and the client anywhere else
on the network: the server is discovered automatically, the first connection
is approved with one click on the server side, and from then on the client
reconnects instantly whenever the server is running. The screen stream is
pixel-exact at full resolution — built for documents, code, and terminals
rather than video — captured with DXGI desktop duplication and delta-compressed
so only the parts of the screen that changed are sent. No Windows RDP, no
Microsoft accounts, no cloud: just two apps and your LAN.

## See it in action

### Client

![Client demo](https://raw.githubusercontent.com/jamesabel/remotedesktop/master/docs/media/client-demo.gif)

The client discovers the server on the LAN, connects, and streams its
desktop live in the *Remote Screen* tab — click into the view and your mouse
and keyboard control the remote machine. The *Performance* tab graphs
bandwidth and round-trip time with live statistics.

### Server

![Server demo](https://raw.githubusercontent.com/jamesabel/remotedesktop/master/docs/media/server-demo.gif)

The server's *Status* tab shows every connected viewer — who they are (login
name, computer, OS) and how the connection is doing (bandwidth, round-trip
time with mean/min/max/p99/jitter over the recent window).

## Features

- 🔍 **Autodiscovery** — servers announce themselves over UDP; the client lists every server on the LAN, no addresses to type.
- 🖥️ **Lossless screen sharing** — pixel-exact at full resolution, DXGI desktop-duplication capture (~10 ms per 4K frame), and inter-frame delta compression: an unchanged screen sends nothing.
- ⌨️🖱️ **Full input control** — mouse, wheel, and keyboard forwarding that is safe against interruptions: anything still held down is released on the server if the viewer loses focus or disconnects, so no stuck keys.
- 📋 **Two-way clipboard** — text and images copied on either machine appear on the other.
- 🔒 **TLS + approve-once pairing** — every connection is encrypted; the server user approves a new client once, after which it reconnects with a stored token and no prompt.
- 📊 **Built-in performance monitoring** — live bandwidth and round-trip-time graphs with window statistics (mean/min/max/p99/jitter), plus a per-viewer table on the server.
- 🔁 **Robust connections** — dead connections are detected and dropped within seconds, and approved clients reconnect automatically without ceremony.
- 🚀 **Hands-off operation** — optional start-at-login (per-user, no admin rights) and a *Restart server* button usable from the remote session itself, so you can update the software without visiting the machine.
- 🗃️ **Persistent peer inventory** — both apps keep a SQLite-backed history of every peer seen on the LAN, with one-click *revoke access* / *forget server*.

In scope: screen, keyboard, mouse, and clipboard. Out of scope: shared
drives, devices, and audio — and smooth playback of fast-changing
full-screen content (video, games) is a non-goal; the stream is optimized
for mostly-static desktop work.

## Installation

```
pip install remotedesktop
```

or, with [uv](https://docs.astral.sh/uv/):

```
uv tool install remotedesktop
```

Or run straight from a clone of this repository: double-click
`run_server.bat` on the computer to be shared and `run_client.bat` on the
viewing computer — each prepares the environment on first use and launches
the app.

## Quick start

1. On the computer to share, run `remotedesktop-server`.
2. On the viewing computer, run `remotedesktop-client` — the server appears
   in its *Servers* panel; double-click it.
3. Approve the connection in the dialog that pops up on the server. That's
   it — future connections from that client need no approval.

From a terminal in a clone of this repo, the same apps run with
`uv run remotedesktop-server` and `uv run remotedesktop-client`.

## Security model

Connections are TLS-encrypted with a self-signed certificate the server
generates once and keeps. The trust model is tuned for a **trusted LAN**:
certificates are trusted on first use and a changed fingerprint is logged
rather than blocking the connection, favoring reliable reconnection over
strict certificate checking. Unapproved clients are limited to small
handshake messages until the server user admits them; access can be revoked
at any time from the server's *Clients on LAN* tab. There is no dependency
on Windows RDP or any Microsoft-based authentication.

## Versioning

The apps follow [semantic versioning](https://semver.org): the **major**
version is the client/server compatibility contract (same major →
guaranteed to interoperate), **minor** versions add backward-compatible
features, **patch** versions fix bugs. On every connection each side
compares its major version with the peer's; a mismatch shows a strong
warning in both GUIs — connecting is still allowed, but the experience is
not guaranteed. Keep both computers on the same version for best results.

## Requirements

- Windows
- Python 3.14+

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

The demo GIFs above are generated — entirely from synthetic data — by
`uv run python tools/make_demo_gifs.py`.

### The `badges` branch

The coverage badge above is served from the `badges` branch
(`raw.githubusercontent.com/.../badges/coverage.svg`). CI regenerates the
SVG after each test run on `master` and force-pushes it there as a single
orphan commit. It lives on its own branch because `master` only accepts
pull requests (a repository ruleset), so CI cannot commit to it directly;
keeping the badge in the repo avoids depending on an external coverage
service. The branch is generated output — never branch from it or merge it.
