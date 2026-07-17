"""Persistent per-machine state, all stored in the shared SQLite database.

`Settings` is a generic key/value store (and holds this client's stable id and
name). `PairedClients` maps each approved client id to the token issued at
approval (server side). `KnownServers` records the servers this machine has
paired with — pinned fingerprint and token — keyed by "host:port" (client
side). All three operate on a `sqlite3.Connection` from `db.connect`.
"""

import socket
import sqlite3
import uuid
from pathlib import Path

import platformdirs


def default_config_dir() -> Path:
    return Path(platformdirs.user_data_dir("remotedesktop", appauthor=False))


def default_db_path() -> Path:
    return default_config_dir() / "remotedesktop.db"


class Settings:
    """A key/value store over the `settings` table."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self._db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row is not None else default

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()


def load_client_identity(connection: sqlite3.Connection) -> tuple[str, str]:
    """Return this client's (stable id, display name), creating it on first use."""
    settings = Settings(connection)
    client_id = settings.get("client_id")
    name = settings.get("client_name")
    if client_id and name:
        return client_id, name
    client_id = str(uuid.uuid4())
    name = socket.gethostname()
    settings.set("client_id", client_id)
    settings.set("client_name", name)
    return client_id, name


class PairedClients:
    """Maps each approved client id to its shared token (server side).

    A client is "approved" exactly when it has a token here.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def __contains__(self, client_id: str) -> bool:
        return self.token_for(client_id) is not None

    def token_for(self, client_id: str) -> str | None:
        row = self._db.execute(
            "SELECT token FROM paired_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return row[0] if row is not None else None

    def pair(self, client_id: str) -> str:
        """Issue and persist a new token for a client, returning it."""
        import secrets

        token = secrets.token_hex(32)
        self._db.execute(
            "INSERT INTO paired_clients (client_id, token) VALUES (?, ?) "
            "ON CONFLICT(client_id) DO UPDATE SET token = excluded.token",
            (client_id, token),
        )
        self._db.commit()
        return token

    def revoke(self, client_id: str) -> None:
        """Remove a client's token so it must be approved again to reconnect."""
        self._db.execute("DELETE FROM paired_clients WHERE client_id = ?", (client_id,))
        self._db.commit()


class KnownServers:
    """Client-side record of paired servers, keyed by "host:port"."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    def get(self, key: str) -> dict | None:
        row = self._db.execute(
            "SELECT fingerprint, token FROM known_servers WHERE key = ?", (key,)
        ).fetchone()
        return {"fingerprint": row[0], "token": row[1]} if row is not None else None

    def remember(self, key: str, fingerprint: str, token: str) -> None:
        self._db.execute(
            "INSERT INTO known_servers (key, fingerprint, token) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET fingerprint = excluded.fingerprint, "
            "token = excluded.token",
            (key, fingerprint, token),
        )
        self._db.commit()

    def forget(self, key: str) -> None:
        """Drop a server's stored token and pin, so the next connection re-pairs."""
        self._db.execute("DELETE FROM known_servers WHERE key = ?", (key,))
        self._db.commit()
