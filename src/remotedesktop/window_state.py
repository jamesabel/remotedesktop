"""Persist and restore top-level window geometry via the settings table.

Both GUI apps save their main window's geometry (size, position, maximized
state) on close and restore it on the next start. Each app uses its own
settings key so a machine running both doesn't mix them up.
"""

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QWidget

from remotedesktop.config import Settings

CLIENT_GEOMETRY_KEY = "client_window_geometry"
SERVER_GEOMETRY_KEY = "server_window_geometry"


def restore_geometry(window: QWidget, settings: Settings, key: str) -> None:
    """Apply the geometry stored under `key`, if any (call before show())."""
    stored = settings.get(key)
    if stored:
        window.restoreGeometry(QByteArray.fromHex(stored.encode()))


def save_geometry(window: QWidget, settings: Settings, key: str) -> None:
    """Store the window's current geometry under `key`."""
    settings.set(key, bytes(window.saveGeometry().toHex()).decode())
