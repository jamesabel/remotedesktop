from PySide6.QtCore import QObject, Signal

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
