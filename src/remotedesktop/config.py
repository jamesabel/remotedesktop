"""Persistent per-machine state under %APPDATA%/remotedesktop.

The client keeps a stable random ID so servers can recognize it across
reconnects, plus the per-server tokens and pinned fingerprints it has paired
with. The server keeps, per approved client ID, the shared token issued when
its user first approved that client.
"""

import json
import os
import secrets
import socket
import uuid
from pathlib import Path


def default_config_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "remotedesktop"


def load_client_identity(path: Path | None = None) -> tuple[str, str]:
    """Return this client's (stable id, display name), creating it on first use."""
    path = path or default_config_dir() / "client_identity.json"
    try:
        data = json.loads(path.read_text())
        return str(data["client_id"]), str(data["name"])
    except (OSError, ValueError, KeyError):
        pass
    client_id = str(uuid.uuid4())
    name = socket.gethostname()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client_id": client_id, "name": name}))
    return client_id, name


class PairedClients:
    """Maps each approved client ID to the shared token issued at approval.

    A client is "approved" exactly when it has a token here. Persisted to disk.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_config_dir() / "paired_clients.json"
        try:
            data = json.loads(self._path.read_text())
            self._tokens = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            self._tokens = {}

    def __contains__(self, client_id: str) -> bool:
        return client_id in self._tokens

    def token_for(self, client_id: str) -> str | None:
        return self._tokens.get(client_id)

    def pair(self, client_id: str) -> str:
        """Issue and persist a new token for a client, returning it."""
        token = secrets.token_hex(32)
        self._tokens[client_id] = token
        self._save()
        return token

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._tokens))


class KnownServers:
    """Client-side record of servers this machine has paired with, keyed by
    "host:port": the pinned certificate fingerprint and the shared token."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_config_dir() / "known_servers.json"
        try:
            data = json.loads(self._path.read_text())
            self._servers = {str(k): dict(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self._servers = {}

    def get(self, key: str) -> dict | None:
        return self._servers.get(key)

    def remember(self, key: str, fingerprint: str, token: str) -> None:
        self._servers[key] = {"fingerprint": fingerprint, "token": token}
        self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._servers))
