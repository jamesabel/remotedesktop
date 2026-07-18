"""Persist and restore top-level window geometry via the settings table.

The app saves its main window's geometry (size, position, maximized state)
on close and restores it on the next start.
"""

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QWidget

from remotedesktop.config import Settings

MAIN_GEOMETRY_KEY = "main_window_geometry"


def restore_geometry(window: QWidget, settings: Settings, key: str) -> None:
    """Apply the geometry stored under `key`, if any (call before show())."""
    stored = settings.get(key)
    if stored:
        window.restoreGeometry(QByteArray.fromHex(stored.encode()))


def save_geometry(window: QWidget, settings: Settings, key: str) -> None:
    """Store the window's current geometry under `key`."""
    settings.set(key, bytes(window.saveGeometry().toHex()).decode())  # ty: ignore[invalid-argument-type]
