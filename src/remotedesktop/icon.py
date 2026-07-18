"""The application icon, drawn in code.

A monitor with a mouse pointer on the screen — "this desktop is controlled
remotely". Painted with QPainter at every common icon size (no binary asset
to version or package).
"""

import ctypes
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF

_SIZES = (16, 24, 32, 48, 64, 128, 256)
_FRAME = QColor("#37474f")
_ACCENTS = {"app": QColor("#1e88e5")}
# A classic cursor-arrow outline on a 0..17 grid, placed on the screen.
_POINTER = [(0, 0), (0, 14), (4, 11), (7, 17), (9.5, 16), (6.5, 10), (11, 10)]


def _pixmap(size: int, accent: QColor) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64.0
    painter.setPen(Qt.PenStyle.NoPen)
    # Monitor frame, screen, stand, base.
    painter.setBrush(_FRAME)
    painter.drawRoundedRect(QRectF(4 * s, 6 * s, 56 * s, 40 * s), 5 * s, 5 * s)
    painter.setBrush(accent)
    painter.drawRoundedRect(QRectF(9 * s, 11 * s, 46 * s, 30 * s), 2 * s, 2 * s)
    painter.setBrush(_FRAME)
    painter.drawRect(QRectF(28 * s, 46 * s, 8 * s, 6 * s))
    painter.drawRoundedRect(QRectF(18 * s, 52 * s, 28 * s, 5 * s), 2 * s, 2 * s)
    # Mouse pointer on the screen: remote control.
    pointer = QPolygonF(
        [QPointF((22 + x * 1.4) * s, (14 + y * 1.4) * s) for x, y in _POINTER]
    )
    painter.setBrush(QColor("white"))
    painter.drawPolygon(pointer)
    painter.end()
    return pixmap


def app_icon(role: str = "app") -> QIcon:
    """The window/taskbar icon (role "app")."""
    accent = _ACCENTS[role]
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(_pixmap(size, accent))
    return icon


def set_windows_app_id(app_id: str) -> None:
    """Give this process its own Windows taskbar identity.

    Without it, Windows groups the app under the Python interpreter's
    identity and may show the interpreter's icon in the taskbar instead of
    the window icon. Harmless no-op off Windows or on failure.
    """
    if sys.platform != "win32":  # pragma: no cover - Windows is the target
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):  # pragma: no cover - defensive
        pass
