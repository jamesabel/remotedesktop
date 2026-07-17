import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtGui import QKeyEvent  # noqa: F401  (used in future input tests)

from remotedesktop.viewer import ViewerWidget

from test_sharing import make_client, make_server, pump


class RecordingInjector:
    """Stand-in for InputInjector that records calls instead of moving the
    real cursor, so tests never touch the host's input."""

    available = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def button(self, x, y, name, pressed):
        self.calls.append(("button", x, y, name, pressed))

    def wheel(self, x, y, delta):
        self.calls.append(("wheel", x, y, delta))

    def key(self, vk, pressed):
        self.calls.append(("key", vk, pressed))


def test_input_is_injected_on_server(qapp, credentials, tmp_path):
    injector = RecordingInjector()
    server = make_server(credentials, tmp_path, approve=lambda *_: True, injector=injector)
    client = make_client(tmp_path)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        client.send_input({"action": "move", "x": 0.5, "y": 0.25})
        client.send_input({"action": "button", "x": 0.5, "y": 0.25, "button": "left", "pressed": True})
        client.send_input({"action": "key", "vk": 65, "pressed": True})
        client.send_input({"action": "wheel", "x": 0.5, "y": 0.25, "dy": 120})
        pump(qapp, lambda: len(injector.calls) >= 4)
        assert ("move", 0.5, 0.25) in injector.calls
        assert ("button", 0.5, 0.25, "left", True) in injector.calls
        assert ("key", 65, True) in injector.calls
        assert ("wheel", 0.5, 0.25, 120) in injector.calls
    finally:
        client.close()
        server.close()


def test_input_from_unapproved_stream_is_ignored(qapp, credentials, tmp_path):
    injector = RecordingInjector()
    server = make_server(credentials, tmp_path, approve=lambda *_: False, injector=injector)
    client = make_client(tmp_path)
    denied = []
    client.denied.connect(denied.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: denied)
        client.send_input({"action": "move", "x": 0.5, "y": 0.5})
        for _ in range(20):
            qapp.processEvents()
        assert injector.calls == []
    finally:
        client.close()
        server.close()


def test_viewer_maps_coordinates_to_frame(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    # 2:1 frame in a 400x400 widget -> displayed 400x200, centered vertically
    # (y offset 100). The frame center is widget (200, 200).
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    center = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(200, 200), Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    viewer.mouseMoveEvent(center)
    assert events[-1]["action"] == "move"
    assert events[-1]["x"] == pytest.approx(0.5, abs=0.01)
    assert events[-1]["y"] == pytest.approx(0.5, abs=0.01)


def test_viewer_ignores_input_outside_frame(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    # y=10 is in the top letterbox bar (frame starts at y=100).
    outside = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(200, 10), Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    viewer.mouseMoveEvent(outside)
    assert events == []
