import base64

from PySide6.QtCore import QBuffer, QObject, Qt, Signal
from PySide6.QtGui import QImage

from remotedesktop.clipboard import ClipboardSync

from test_sharing import make_client, make_server, pump


class FakeClipboard(QObject):
    """Stand-in for ClipboardSync: records applied payloads and lets a test
    simulate a local copy, without touching the OS clipboard."""

    changed = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self.applied: list[dict] = []

    def apply(self, payload: dict) -> None:
        self.applied.append(payload)

    def local_copy(self, payload: dict) -> None:
        self.changed.emit(payload)


def connected_pair(qapp, credentials, tmp_path, server_cb, client_cb):
    server = make_server(credentials, tmp_path, approve=lambda *_: True, clipboard=server_cb)
    client = make_client(tmp_path, clipboard=client_cb)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    pump(qapp, lambda: connected)
    return server, client


def test_client_copy_reaches_server(qapp, credentials, tmp_path):
    server_cb, client_cb = FakeClipboard(), FakeClipboard()
    server, client = connected_pair(qapp, credentials, tmp_path, server_cb, client_cb)
    try:
        client_cb.local_copy({"text": "hello from client"})
        pump(qapp, lambda: server_cb.applied)
        assert server_cb.applied == [{"type": "clipboard", "text": "hello from client"}]
    finally:
        client.close()
        server.close()


def test_server_copy_reaches_client(qapp, credentials, tmp_path):
    server_cb, client_cb = FakeClipboard(), FakeClipboard()
    server, client = connected_pair(qapp, credentials, tmp_path, server_cb, client_cb)
    try:
        server_cb.local_copy({"text": "hello from server"})
        pump(qapp, lambda: client_cb.applied)
        assert client_cb.applied == [{"type": "clipboard", "text": "hello from server"}]
    finally:
        client.close()
        server.close()


def test_clipboard_from_unapproved_stream_is_ignored(qapp, credentials, tmp_path):
    server_cb = FakeClipboard()
    server = make_server(credentials, tmp_path, approve=lambda *_: False, clipboard=server_cb)
    client = make_client(tmp_path, clipboard=FakeClipboard())
    denied = []
    client.denied.connect(denied.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: denied)
        client._stream.send_json({"type": "clipboard", "text": "sneaky"})
        for _ in range(20):
            qapp.processEvents()
        assert server_cb.applied == []
    finally:
        client.close()
        server.close()


def test_large_clipboard_passes_after_pairing(qapp, credentials, tmp_path):
    # The server caps message size until the handshake completes; an admitted
    # client must be able to send more than that pre-auth cap.
    server_cb, client_cb = FakeClipboard(), FakeClipboard()
    server, client = connected_pair(qapp, credentials, tmp_path, server_cb, client_cb)
    try:
        big = "x" * (256 * 1024)
        client_cb.local_copy({"text": big})
        pump(qapp, lambda: server_cb.applied)
        assert server_cb.applied[0]["text"] == big
    finally:
        client.close()
        server.close()


# --- ClipboardSync unit tests against the real QClipboard (single process) ---


def test_sync_emits_on_local_change(qapp):
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    emitted: list[dict] = []
    sync.changed.connect(emitted.append)
    clip.setText("unit-test-copy")
    pump(qapp, lambda: emitted)  # dataChanged fires asynchronously on Windows
    assert emitted[-1]["text"] == "unit-test-copy"


def test_apply_does_not_echo_back(qapp):
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    emitted: list[dict] = []
    sync.changed.connect(emitted.append)
    sync.apply({"type": "clipboard", "text": "applied-value"})
    for _ in range(10):
        qapp.processEvents()
    assert clip.text() == "applied-value"
    # Applying the remote value must not be re-emitted as a local change.
    assert emitted == []


def _red_image() -> QImage:
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    return image


def test_local_image_copy_is_encoded_as_png(qapp):
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    emitted: list[dict] = []
    sync.changed.connect(emitted.append)
    clip.setImage(_red_image())
    pump(qapp, lambda: emitted)
    encoded = emitted[-1]["image_png"]
    decoded = QImage.fromData(base64.b64decode(encoded), "PNG")  # ty: ignore[invalid-argument-type]
    assert decoded.pixelColor(0, 0).name() == "#ff0000"


def test_apply_image_payload_sets_clipboard_image(qapp):
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    _red_image().save(buffer, "PNG")  # ty: ignore[no-matching-overload]
    payload = {
        "type": "clipboard",
        "text": "caption",
        "image_png": base64.b64encode(
            bytes(buffer.data())  # ty: ignore[invalid-argument-type]
        ).decode(),
    }
    sync.apply(payload)
    assert clip.image().pixelColor(0, 0).name() == "#ff0000"
    # Both halves of a text+image payload survive the round trip.
    assert clip.text() == "caption"
    # Re-applying the identical payload is a no-op (signature match).
    sync.apply(payload)


def test_copy_image_sets_clipboard_without_echoing_to_peers(qapp):
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    emitted: list[dict] = []
    sync.changed.connect(emitted.append)
    sync.copy_image(_red_image())
    for _ in range(10):
        qapp.processEvents()
    assert clip.image().pixelColor(0, 0).name() == "#ff0000"
    # An app-generated copy (a screen capture) is never synced to peers.
    assert emitted == []


def test_copy_image_works_with_sync_disabled(qapp):
    # The Preferences toggle governs syncing; a local capture copy is not sync.
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    sync.enabled = False
    blue = QImage(4, 4, QImage.Format.Format_RGB32)
    blue.fill(Qt.GlobalColor.blue)
    sync.copy_image(blue)
    assert clip.image().pixelColor(0, 0).name() == "#0000ff"


def test_apply_ignores_garbage_payloads(qapp):
    clip = qapp.clipboard()
    clip.setText("before")
    for _ in range(10):
        qapp.processEvents()
    sync = ClipboardSync(clip)
    sync.apply({"type": "clipboard"})
    sync.apply({"type": "clipboard", "image_png": "!!! not base64 !!!"})
    sync.apply({"type": "clipboard", "text": 42})
    assert clip.text() == "before"


def test_data_changed_while_applying_is_ignored(qapp):
    sync = ClipboardSync(qapp.clipboard())
    emitted: list[dict] = []
    sync.changed.connect(emitted.append)
    sync._applying = True
    sync._on_data_changed()
    assert emitted == []


def test_disabled_sync_neither_sends_nor_applies(qapp):
    clip = qapp.clipboard()
    sync = ClipboardSync(clip)
    emitted: list[dict] = []
    sync.changed.connect(emitted.append)
    sync.enabled = False
    clip.setText("disabled-copy")
    for _ in range(10):
        qapp.processEvents()
    assert emitted == []  # local copies are not sent
    sync.apply({"type": "clipboard", "text": "should-not-apply"})
    for _ in range(5):
        qapp.processEvents()
    assert clip.text() == "disabled-copy"  # peer payloads are not applied
    # Re-enabled: local copies flow again.
    sync.enabled = True
    clip.setText("enabled-copy")
    pump(qapp, lambda: emitted)
    assert emitted[-1]["text"] == "enabled-copy"
