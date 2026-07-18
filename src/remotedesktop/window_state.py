"""Persist and restore window geometry and layout via the settings table.

The app saves its main window's geometry (size, position, maximized state)
and layout state (dock visibility/placement) on close and restores them on
the next start. Docks must have an objectName for saveState to include them.
"""

from PySide6.QtCore import QByteArray
from PySide6.QtWidgets import QMainWindow, QWidget

from remotedesktop.config import Settings

MAIN_GEOMETRY_KEY = "main_window_geometry"
MAIN_STATE_KEY = "main_window_state"


def restore_geometry(window: QWidget, settings: Settings, key: str) -> None:
    """Apply the geometry stored under `key`, if any (call before show())."""
    stored = settings.get(key)
    if stored:
        window.restoreGeometry(QByteArray.fromHex(stored.encode()))


def save_geometry(window: QWidget, settings: Settings, key: str) -> None:
    """Store the window's current geometry under `key`."""
    settings.set(key, bytes(window.saveGeometry().toHex()).decode())  # ty: ignore[invalid-argument-type]


def restore_state(window: QMainWindow, settings: Settings, key: str) -> None:
    """Apply the dock/toolbar layout stored under `key`, if any."""
    stored = settings.get(key)
    if stored:
        window.restoreState(QByteArray.fromHex(stored.encode()))


def save_state(window: QMainWindow, settings: Settings, key: str) -> None:
    """Store the window's current dock/toolbar layout under `key`."""
    settings.set(key, bytes(window.saveState().toHex()).decode())  # ty: ignore[invalid-argument-type]
