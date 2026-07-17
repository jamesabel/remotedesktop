"""Persistent per-machine state under %APPDATA%/remotedesktop.

The client keeps a stable random ID so servers can recognize it across
reconnects; the server keeps the set of client IDs its user has approved.
"""

import json
import os
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


class ApprovedClients:
    """The set of client IDs the server-side user has permitted, persisted to disk."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_config_dir() / "approved_clients.json"
        try:
            self._ids = {str(client_id) for client_id in json.loads(self._path.read_text())}
        except (OSError, ValueError, TypeError):
            self._ids = set()

    def __contains__(self, client_id: str) -> bool:
        return client_id in self._ids

    def add(self, client_id: str) -> None:
        self._ids.add(client_id)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(sorted(self._ids)))
