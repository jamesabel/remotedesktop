"""Viewer widget that displays the remote desktop inside the client GUI."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class ViewerWidget(QWidget):
    """Displays the remote desktop screen and will forward keyboard, mouse,
    and clipboard events to the connected server."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._placeholder = QLabel("Not connected", alignment=Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.addWidget(self._placeholder)
