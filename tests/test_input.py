import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QImage, QKeyEvent, QMouseEvent, QWheelEvent

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


def _mouse_event(event_type, pos, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(
        event_type, pos, button, button, Qt.KeyboardModifier.NoModifier
    )


def test_button_release_outside_frame_is_clamped_and_sent(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    # Press inside the frame, release over the top letterbox bar: the release
    # must still be sent (clamped to the frame edge) or the server keeps the
    # button held forever.
    viewer.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(200, 200)))
    viewer.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(200, 10)))
    assert [e["pressed"] for e in events] == [True, False]
    release = events[-1]
    assert release["action"] == "button" and release["button"] == "left"
    assert release["x"] == pytest.approx(0.5, abs=0.01)
    assert release["y"] == pytest.approx(0.0)


def test_release_without_press_is_ignored(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    viewer.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(200, 200)))
    assert events == []


def test_focus_out_releases_held_keys_and_buttons(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    viewer.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(200, 200)))
    viewer.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, 0, 65, 0)
    )
    viewer.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    releases = [e for e in events if not e["pressed"]]
    assert {"action": "button", "button": "left", "pressed": False} in releases
    assert {"action": "key", "vk": 65, "pressed": False} in releases
    # Everything was released once; a second focus-out has nothing to add.
    viewer.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    assert len(events) == 4


def test_server_releases_stuck_input_when_client_disconnects(qapp, credentials, tmp_path):
    injector = RecordingInjector()
    server = make_server(credentials, tmp_path, approve=lambda *_: True, injector=injector)
    client = make_client(tmp_path)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        client.send_input({"action": "button", "x": 0.5, "y": 0.5, "button": "left", "pressed": True})
        client.send_input({"action": "key", "vk": 65, "pressed": True})
        pump(qapp, lambda: len(injector.calls) >= 2)
        client.close()
        pump(qapp, lambda: ("key", 65, False) in injector.calls)
        assert ("button", None, None, "left", False) in injector.calls
    finally:
        client.close()
        server.close()


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


def test_viewer_forwards_wheel_and_key_release(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    wheel = QWheelEvent(
        QPointF(200, 200), QPointF(200, 200), QPoint(0, 0), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    viewer.wheelEvent(wheel)
    assert events[-1] == {
        "action": "wheel", "dy": -120,
        "x": pytest.approx(0.5, abs=0.01), "y": pytest.approx(0.5, abs=0.01),
    }
    viewer.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, 0, 65, 0)
    )
    viewer.keyReleaseEvent(
        QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, 0, 65, 0)
    )
    assert events[-1] == {"action": "key", "vk": 65, "pressed": False}
    assert viewer._pressed_keys == set()
    # Keys without a native VK (rare synthetic events) are dropped.
    viewer.keyPressEvent(
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, 0, 0, 0)
    )
    assert events[-1]["pressed"] is False


def test_tab_key_is_forwarded_not_used_for_focus_traversal(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    # Send Tab through the real event path: QWidget.event() offers it to
    # focusNextPrevChild before keyPressEvent, which is what ate Tab.
    tab = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier, 0, 0x09, 0)
    qapp.sendEvent(viewer, tab)
    assert events == [{"action": "key", "vk": 0x09, "pressed": True}]
    # Shift+Tab arrives as Backtab and must be forwarded the same way.
    backtab = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier, 0, 0x09, 0
    )
    qapp.sendEvent(viewer, backtab)
    assert events[-1] == {"action": "key", "vk": 0x09, "pressed": True}


def test_release_when_frame_vanished_mid_drag(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    events: list[dict] = []
    viewer.inputEvent.connect(events.append)
    viewer.mousePressEvent(_mouse_event(QEvent.Type.MouseButtonPress, QPointF(200, 200)))
    viewer._frame = None  # frame dropped mid-drag; server releases on drop
    viewer.mouseReleaseEvent(_mouse_event(QEvent.Type.MouseButtonRelease, QPointF(200, 200)))
    assert [e["pressed"] for e in events] == [True]


def test_viewer_mirrors_remote_cursor_shape(qapp):
    viewer = ViewerWidget()
    viewer.set_remote_cursor("size_we")
    assert viewer.cursor().shape() == Qt.CursorShape.SizeHorCursor
    viewer.set_remote_cursor("hidden")
    assert viewer.cursor().shape() == Qt.CursorShape.BlankCursor
    # A name from a newer server's vocabulary falls back to the arrow.
    viewer.set_remote_cursor("lasso")
    assert viewer.cursor().shape() == Qt.CursorShape.ArrowCursor
    # Disconnecting restores the normal local cursor.
    viewer.set_remote_cursor("size_ns")
    viewer.clear()
    assert viewer.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_viewer_paints_message_and_frame(qapp):
    viewer = ViewerWidget()
    viewer.resize(400, 400)
    assert not viewer.has_frame
    blank = viewer.grab()  # paints the "Not connected" message branch
    assert not blank.isNull()
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    viewer.show_frame(image)
    assert viewer.has_frame
    painted = viewer.grab().toImage()  # paints the frame branch
    assert painted.pixelColor(200, 200).red() > 200  # frame center is red
    viewer.clear("gone")
    assert not viewer.has_frame


def test_shortcut_override_forwards_keys_only_while_a_frame_is_shown(qapp):
    from PySide6.QtWidgets import QApplication

    viewer = ViewerWidget()

    def override_accepted(key, modifiers=Qt.KeyboardModifier.NoModifier):
        event = QKeyEvent(QEvent.Type.ShortcutOverride, key, modifiers)
        event.ignore()  # Qt sends overrides unaccepted; accepting claims the key
        QApplication.sendEvent(viewer, event)
        return event.isAccepted()

    # No frame: local shortcuts must work normally.
    assert not override_accepted(Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    viewer.show_frame(QImage(8, 8, QImage.Format.Format_RGB32))
    # With a frame: keys are claimed for the remote machine.
    assert override_accepted(Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    assert override_accepted(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    assert override_accepted(Qt.Key.Key_F5)
    # F11 is the single key reserved for the local app (fullscreen).
    assert not override_accepted(Qt.Key.Key_F11)
    viewer.clear()
    assert not override_accepted(Qt.Key.Key_F5)


def test_actual_size_mode_sizes_viewer_to_frame_and_maps_input_one_to_one(qapp):
    from PySide6.QtCore import QSize

    viewer = ViewerWidget()
    viewer.set_actual_size(True)
    viewer.show_frame(QImage(200, 100, QImage.Format.Format_RGB32))
    dpr = viewer.devicePixelRatioF()
    expected = QSize(round(200 / dpr), round(100 / dpr))
    assert viewer.sizeHint() == expected
    assert viewer.size() == expected
    # The frame fills the widget exactly, so input maps 1:1.
    center = viewer._normalized(QPointF(viewer.width() / 2, viewer.height() / 2))
    assert center is not None
    assert abs(center[0] - 0.5) < 0.01 and abs(center[1] - 0.5) < 0.01
    # A new frame at a different resolution re-sizes the widget.
    viewer.show_frame(QImage(300, 150, QImage.Format.Format_RGB32))
    assert viewer.size() == QSize(round(300 / dpr), round(150 / dpr))
    # Back to fit mode restores the minimum size.
    viewer.set_actual_size(False)
    assert viewer.minimumSize() == QSize(320, 240)


def test_view_only_drops_input_and_releases_held_keys(qapp, credentials, tmp_path):
    import time

    injector = RecordingInjector()
    server = make_server(credentials, tmp_path, approve=lambda *_: True, injector=injector)
    client = make_client(tmp_path)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        client.send_input({"action": "key", "vk": 65, "pressed": True})
        pump(qapp, lambda: ("key", 65, True) in injector.calls)

        server.set_input_allowed(False)
        # Turning input off released the key the viewer still held.
        assert ("key", 65, False) in injector.calls
        baseline = len(injector.calls)
        client.send_input({"action": "move", "x": 0.5, "y": 0.5})
        client.send_input({"action": "key", "vk": 66, "pressed": True})
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert len(injector.calls) == baseline  # view-only: nothing injected

        server.set_input_allowed(True)
        client.send_input({"action": "key", "vk": 67, "pressed": True})
        pump(qapp, lambda: ("key", 67, True) in injector.calls)
    finally:
        client.close()
        server.close()
