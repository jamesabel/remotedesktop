"""Viewer widget that displays the remote desktop inside the client GUI."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPaintEvent, QPainter, QPixmap
from PySide6.QtWidgets import QWidget


class ViewerWidget(QWidget):
    """Displays the remote desktop screen, scaled to fit; keyboard, mouse,
    and clipboard forwarding will live here too."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame: QPixmap | None = None
        self._message = "Not connected"
        self.setMinimumSize(320, 240)

    @property
    def has_frame(self) -> bool:
        return self._frame is not None

    def show_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self.update()

    def clear(self, message: str = "Not connected") -> None:
        self._frame = None
        self._message = message
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._frame is None:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            return
        scaled = self._frame.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
