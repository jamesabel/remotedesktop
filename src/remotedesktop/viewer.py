"""Viewer widget that displays the remote desktop and forwards input to it.

While a frame is shown, every key belongs to the remote machine: the widget
accepts `ShortcutOverride` events so the window's menu shortcuts (Ctrl+W,
Ctrl+Q, F5, …) never swallow keys meant for the remote desktop. **F11 is the
single key reserved for the local app** (the fullscreen toggle) — it is never
forwarded.
"""

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPalette,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

# Frame-outline shades: light gray on a dark theme, dark gray on a light one,
# so the border contrasts with the app background on either side of the edge.
_BORDER_ON_DARK = QColor(170, 170, 170)
_BORDER_ON_LIGHT = QColor(110, 110, 110)

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
        # Actual-size mode: the widget sizes itself to the frame's device
        # pixels (its host scroll area shows scrollbars) instead of scaling
        # the frame down to fit.
        self._actual_size = False
        # The frame stays at the server's full resolution; what is painted is
        # a SmoothTransformation-scaled copy (proper filtering, unlike the
        # nearest-neighbor scaling of drawPixmap into a rect), cached until
        # the frame or the display size changes. It is scaled to *device*
        # pixels and stamped with the devicePixelRatio: sizing it in logical
        # pixels would make Qt bilinear-upscale it again by the Windows
        # display-scaling factor (125-150% on most monitors), blurring text.
        self._scaled: QPixmap | None = None
        self._message = "Not connected"
        # Buttons/keys currently held, so releases can be forwarded even when
        # they happen outside the frame or when the widget loses focus —
        # otherwise the server would keep them pressed forever.
        self._pressed_buttons: set[str] = set()
        self._pressed_keys: set[int] = set()
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    @property
    def has_frame(self) -> bool:
        return self._frame is not None

    def set_actual_size(self, enabled: bool) -> None:
        """Toggle 1:1 device-pixel display (host provides the scrolling)."""
        if self._actual_size == enabled:
            return
        self._actual_size = enabled
        self._scaled = None
        if enabled:
            self.setMinimumSize(0, 0)
            self._resize_to_frame()
        else:
            self.setMinimumSize(320, 240)
        self.updateGeometry()
        self.update()

    def _resize_to_frame(self) -> None:
        if self._frame is not None:
            self.resize(self.sizeHint())

    def sizeHint(self) -> QSize:
        if self._actual_size and self._frame is not None:
            # Logical size that maps to exactly one device pixel per frame
            # pixel, so the paint path hits its no-resample branch.
            dpr = self.devicePixelRatioF()
            return QSize(round(self._frame.width() / dpr), round(self._frame.height() / dpr))
        return super().sizeHint()

    def show_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self._scaled = None
        if self._actual_size:
            self._resize_to_frame()  # follows remote resolution changes
        self.update()

    def clear(self, message: str = "Not connected") -> None:
        self._frame = None
        self._scaled = None
        self._message = message
        self._pressed_buttons.clear()
        self._pressed_keys.clear()
        self.update()

    def _display_rect(self) -> QRectF:
        """Rectangle the frame occupies, centered and aspect-preserved."""
        assert self._frame is not None
        size = self._frame.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - size.width()) / 2
        y = (self.height() - size.height()) / 2
        return QRectF(x, y, size.width(), size.height())

    def _normalized(self, pos: QPointF, *, clamp: bool = False) -> tuple[float, float] | None:
        """Map a widget position to 0..1 over the frame, or None if outside it.

        With `clamp=True` a position outside the frame maps to the nearest
        frame edge instead of None (used for button releases, which must
        reach the server even when the drag ended past the frame).
        """
        if self._frame is None:
            return None
        rect = self._display_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        if not clamp and not rect.contains(pos):
            return None
        x = (pos.x() - rect.x()) / rect.width()
        y = (pos.y() - rect.y()) / rect.height()
        if clamp:
            x = min(1.0, max(0.0, x))
            y = min(1.0, max(0.0, y))
        return (x, y)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        coords = self._normalized(event.position())
        if coords is not None:
            self.inputEvent.emit({"action": "move", "x": coords[0], "y": coords[1]})

    def _button_event(self, event: QMouseEvent, pressed: bool) -> None:
        name = _BUTTON_NAMES.get(event.button())
        if name is None:
            return
        if pressed:
            coords = self._normalized(event.position())
            if coords is None:
                return
            self._pressed_buttons.add(name)
        else:
            if name not in self._pressed_buttons:
                return
            self._pressed_buttons.discard(name)
            coords = self._normalized(event.position(), clamp=True)
            if coords is None:  # frame gone mid-drag; server releases on drop
                return
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
        if not vk:
            return
        vk = int(vk)
        if pressed:
            self._pressed_keys.add(vk)
        else:
            self._pressed_keys.discard(vk)
        self.inputEvent.emit({"action": "key", "vk": vk, "pressed": pressed})

    def event(self, event) -> bool:
        # See the module docstring: with a frame present, accepting the
        # override makes Qt redeliver the key as an ordinary KeyPress to
        # this widget (which forwards it) instead of firing a local
        # shortcut. Without a frame, local shortcuts work normally.
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and self._frame is not None
            and event.key() != Qt.Key.Key_F11
        ):
            event.accept()
            return True
        return super().event(event)

    def focusNextPrevChild(self, next: bool) -> bool:
        # Qt consumes Tab/Shift+Tab for focus traversal before keyPressEvent
        # ever runs; declining here makes them arrive as ordinary key events
        # so they reach the remote desktop instead of moving local focus.
        if self._frame is not None:
            return False
        return super().focusNextPrevChild(next)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self._key_event(event, False)

    def release_input(self) -> None:
        """Emit release events for everything currently held down.

        Button releases carry no coordinates, so the server releases them at
        the cursor's current position instead of jerking the pointer.
        """
        for name in sorted(self._pressed_buttons):
            self.inputEvent.emit({"action": "button", "button": name, "pressed": False})
        self._pressed_buttons.clear()
        for vk in sorted(self._pressed_keys):
            self.inputEvent.emit({"action": "key", "vk": vk, "pressed": False})
        self._pressed_keys.clear()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        # Alt-Tab (or any focus loss) means key/button releases will go to
        # another window; release everything on the server side first.
        self.release_input()
        super().focusOutEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._frame is None:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            return
        rect = self._display_rect().toRect()
        dpr = self.devicePixelRatioF()
        target = QSize(round(rect.width() * dpr), round(rect.height() * dpr))
        if (
            self._scaled is None
            or self._scaled.size() != target
            or self._scaled.devicePixelRatio() != dpr
        ):
            if self._frame.size() == target:
                # Displayed at exactly 1:1 device pixels — no resample at all.
                self._scaled = QPixmap(self._frame)
            else:
                self._scaled = self._frame.scaled(
                    target,
                    Qt.AspectRatioMode.IgnoreAspectRatio,  # rect is already aspect-correct
                    Qt.TransformationMode.SmoothTransformation,
                )
            self._scaled.setDevicePixelRatio(dpr)
        painter.drawPixmap(rect.topLeft(), self._scaled)
        # Thin outline marking where the remote screen ends and the app
        # background begins (they can otherwise blend together).
        painter.setPen(self._border_color())
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def _border_color(self) -> QColor:
        """Outline shade for the current theme (follows the window background)."""
        window = self.palette().color(QPalette.ColorRole.Window)
        return _BORDER_ON_DARK if window.lightness() < 128 else _BORDER_ON_LIGHT
