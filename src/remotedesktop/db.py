"""The single SQLite database that backs all persistence.

One database file under %LOCALAPPDATA%/remotedesktop holds every persistent table:
settings (a key/value store, including this client's identity), the server's
paired clients, the client's known servers, and the connection inventory. Pass
`None` for an in-memory database (used by tests).
"""

import sqlite3
from pathlib import Path

_PEER_COLUMNS = (
    "key TEXT PRIMARY KEY, name TEXT, address TEXT, detail TEXT, "
    "first_seen TEXT, last_seen TEXT, attempts INTEGER, state TEXT, last_event TEXT"
)

# The server's inventory of clients and the client's inventory of servers are
# kept in separate tables so a machine running both apps doesn't commingle them.
# `peers` is the generic table used by tests.
PEER_TABLES = ("peers", "server_peers", "client_peers")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS paired_clients (
    client_id TEXT PRIMARY KEY,
    token TEXT
);
CREATE TABLE IF NOT EXISTS known_servers (
    key TEXT PRIMARY KEY,
    fingerprint TEXT,
    token TEXT
);
CREATE TABLE IF NOT EXISTS peers ({_PEER_COLUMNS});
CREATE TABLE IF NOT EXISTS server_peers ({_PEER_COLUMNS});
CREATE TABLE IF NOT EXISTS client_peers ({_PEER_COLUMNS});
"""


def connect(path: Path | None) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure every table exists."""
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path) if path is not None else ":memory:")
    connection.executescript(_SCHEMA)
    connection.commit()
    return connection
