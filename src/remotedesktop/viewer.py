"""Viewer widget that displays the remote desktop and forwards input to it."""

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

_BUTTON_NAMES = {
    Qt.MouseButton.LeftButton: "left",
    Qt.MouseButton.RightButton: "right",
    Qt.MouseButton.MiddleButton: "middle",
}


class ViewerWidget(QWidget):
    """Displays the remote desktop screen scaled to fit, and forwards mouse and
    keyboard events (as normalized 0..1 coordinates) to the connected server."""

    inputEvent = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame: QPixmap | None = None
        self._message = "Not connected"
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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

    def _display_rect(self) -> QRectF:
        """Rectangle the frame occupies, centered and aspect-preserved."""
        assert self._frame is not None
        size = self._frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - size.width()) / 2
        y = (self.height() - size.height()) / 2
        return QRectF(x, y, size.width(), size.height())

    def _normalized(self, pos: QPointF) -> tuple[float, float] | None:
        """Map a widget position to 0..1 over the frame, or None if outside it."""
        if self._frame is None:
            return None
        rect = self._display_rect()
        if not rect.contains(pos) or rect.width() <= 0 or rect.height() <= 0:
            return None
        return (
            (pos.x() - rect.x()) / rect.width(),
            (pos.y() - rect.y()) / rect.height(),
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        coords = self._normalized(event.position())
        if coords is not None:
            self.inputEvent.emit({"action": "move", "x": coords[0], "y": coords[1]})

    def _button_event(self, event: QMouseEvent, pressed: bool) -> None:
        name = _BUTTON_NAMES.get(event.button())
        coords = self._normalized(event.position())
        if name is not None and coords is not None:
            self.inputEvent.emit(
                {"action": "button", "button": name, "pressed": pressed,
                 "x": coords[0], "y": coords[1]}
            )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.setFocus()
        self._button_event(event, True)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._button_event(event, False)

    def wheelEvent(self, event: QWheelEvent) -> None:
        coords = self._normalized(event.position())
        delta = event.angleDelta().y()
        if coords is not None and delta:
            self.inputEvent.emit(
                {"action": "wheel", "dy": delta, "x": coords[0], "y": coords[1]}
            )

    def _key_event(self, event: QKeyEvent, pressed: bool) -> None:
        vk = event.nativeVirtualKey()
        if vk:
            self.inputEvent.emit({"action": "key", "vk": int(vk), "pressed": pressed})

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, False)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._frame is None:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            return
        painter.drawPixmap(self._display_rect().toRect(), self._frame)
