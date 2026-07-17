import socket

from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import QMessageBox

from remotedesktop import db
from remotedesktop.autostart import Autostart
from remotedesktop.config import PairedClients
from remotedesktop.server import ServerWindow

from test_discovery import free_udp_port
from test_sharing import CLIENT_ID, make_client, pump

_TEST_AUTOSTART_KEY = r"Software\remotedesktop-tests\WindowRun"


def make_window(credentials, tmp_path, *, discovery_port=None, connect_port=0):
    connection = db.connect(tmp_path / "server.db")
    return ServerWindow(
        discovery_port=discovery_port if discovery_port is not None else free_udp_port(),
        connect_port=connect_port,
        paired=PairedClients(connection),
        credentials=credentials,
        connection=connection,
        autostart=Autostart(key_path=_TEST_AUTOSTART_KEY, value_name="window-test"),
    )


def test_window_listens_and_is_discoverable(qapp, credentials, tmp_path):
    window = make_window(credentials, tmp_path)
    try:
        assert window._listening
        assert window._discoverable
        assert "Discoverable on this LAN" in window._summary.text()
        assert "Not sharing" in window._summary.text()
    finally:
        window.close()


def test_window_reports_discovery_port_conflict(qapp, credentials, tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("", 0))
    port = blocker.getsockname()[1]
    try:
        window = make_window(credentials, tmp_path, discovery_port=port)
        try:
            assert window._listening
            assert not window._discoverable
            assert "Not discoverable" in window._summary.text()
            assert "Discovery unavailable" in window.connection_log.toPlainText()
        finally:
            window.close()
    finally:
        blocker.close()


def test_window_reports_connect_port_conflict(qapp, credentials, tmp_path):
    # Block the port the same way the app binds it (dual-stack Any via Qt);
    # a raw IPv4-only socket would not conflict with Qt's IPv6 Any binding.
    blocker = QTcpServer()
    assert blocker.listen(QHostAddress.SpecialAddress.Any, 0)
    port = blocker.serverPort()
    try:
        window = make_window(credentials, tmp_path, connect_port=port)
        try:
            assert not window._listening
            assert window.responder is None
            assert "Cannot share" in window._summary.text()
        finally:
            window.close()
    finally:
        blocker.close()


def test_autostart_checkbox_toggles_registration(qapp, credentials, tmp_path):
    window = make_window(credentials, tmp_path)
    try:
        assert not window._autostart.is_enabled()
        window.autostart_checkbox.setChecked(True)
        assert window._autostart.is_enabled()
        assert "start at login" in window.connection_log.toPlainText()
        window.autostart_checkbox.setChecked(False)
        assert not window._autostart.is_enabled()
    finally:
        window.close()


def test_approval_prompt_pairs_client_and_updates_summary(qapp, credentials, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window = make_window(credentials, tmp_path)
    client = make_client(tmp_path)
    names = []
    client.connected.connect(names.append)
    client.connect_to("127.0.0.1", window.share_server.port)
    try:
        pump(qapp, lambda: names)
        pump(qapp, lambda: "Sharing this desktop with 1 viewer(s)" in window._summary.text())
        # The inventory tab tracked the pairing via peer events.
        assert window.inventory._peers[CLIENT_ID].state == "connected (paired)"
    finally:
        client.close()
        window.close()


def test_refused_approval_denies_client(qapp, credentials, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    window = make_window(credentials, tmp_path)
    client = make_client(tmp_path)
    denials = []
    client.denied.connect(denials.append)
    client.connect_to("127.0.0.1", window.share_server.port)
    try:
        pump(qapp, lambda: denials)
        assert "refused" in denials[0]
    finally:
        client.close()
        window.close()


def test_revoke_via_window_disconnects_client(qapp, credentials, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    window = make_window(credentials, tmp_path)
    client = make_client(tmp_path)
    names, disconnected = [], []
    client.connected.connect(names.append)
    client.disconnected.connect(lambda: disconnected.append(True))
    client.connect_to("127.0.0.1", window.share_server.port)
    try:
        pump(qapp, lambda: names)
        window._revoke_client(CLIENT_ID)
        pump(qapp, lambda: disconnected)
        assert window.inventory._peers[CLIENT_ID].state == "revoked"

        # Answering "No" must not revoke anything.
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
        )
        window._revoke_client(CLIENT_ID)
    finally:
        client.close()
        window.close()
