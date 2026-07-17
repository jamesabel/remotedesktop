from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidgetItem, QMessageBox

from remotedesktop import client as client_module
from remotedesktop import db
from remotedesktop.client import ClientWindow, DiscoveryPanel
from remotedesktop.config import PairedClients
from remotedesktop.discovery import ServerInfo
from remotedesktop.sharing import ShareServer

from test_sharing import pump


def make_window(tmp_path):
    # auto_scan=False: window tests must never broadcast a discovery probe on the LAN.
    return ClientWindow(connection=db.connect(tmp_path / "client.db"), auto_scan=False)


def make_share_server(credentials, tmp_path, *, approve=lambda *_: True):
    server = ShareServer(
        approve_client=approve,
        credentials=credentials,
        paired=PairedClients(db.connect(tmp_path / "server.db")),
    )
    assert server.listen(0)
    return server


def test_get_server_log_button_needs_a_connection(qapp, tmp_path):
    window = make_window(tmp_path)
    try:
        window.get_log_button.click()
        assert "no server to request a log from" in window.connection_log.toPlainText()
    finally:
        window.close()


def test_received_server_log_opens_a_viewer_dialog(qapp, tmp_path):
    from remotedesktop.logs import PeerLogDialog

    window = make_window(tmp_path)
    try:
        window._show_server_log("some log text")
        dialog = window.findChild(PeerLogDialog)
        assert dialog is not None
        assert "Log from server" in dialog.windowTitle()
        dialog.close()
    finally:
        window.close()


def test_window_starts_disconnected(qapp, tmp_path):
    window = make_window(tmp_path)
    assert window.statusBar().currentMessage() == "Not connected"
    assert "Client started" in window.connection_log.toPlainText()
    tabs = window.centralWidget()
    labels = [tabs.tabText(i) for i in range(tabs.count())]
    assert "Performance" in labels and "Preferences" in labels
    assert not window.performance._timer.isActive()  # idle: no periodic work
    assert not window.windowIcon().isNull()
    window.close()


def test_discovered_servers_are_recorded(qapp, tmp_path):
    window = make_window(tmp_path)
    info = ServerInfo(name="box", host="10.0.0.7", port=1234)
    window._record_discovered([info])
    peers = window.inventory.peers()
    assert len(peers) == 1
    assert peers[0].state == "discovered"
    window.close()


def test_connect_view_and_disconnect_full_flow(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    info = ServerInfo(name="box", host="127.0.0.1", port=server.port)
    try:
        window._on_server_activated(info)
        pump(qapp, lambda: window.viewer.has_frame)
        assert window._connected
        assert "Viewing" in window.statusBar().currentMessage()
        key = f"127.0.0.1:{server.port}"
        assert window.inventory._peers[key].state == "connected"

        # Input from the viewer is forwarded while connected.
        window._on_input_event({"action": "move", "x": 0.5, "y": 0.5})

        # A re-scan that finds the connected server must not downgrade it.
        window._record_discovered([info])
        assert window.inventory._peers[key].state == "connected"

        server.close()
        pump(qapp, lambda: not window._connected)
        assert window.inventory._peers[key].state == "disconnected"
    finally:
        window.close()
        server.close()


def test_waiting_for_approval_is_shown_while_server_prompts(qapp, credentials, tmp_path):
    window = make_window(tmp_path)

    def approve(cid, name):
        # While the server-side prompt is "open", the client window must show
        # the waiting state (pump until the pending message arrives).
        pump(
            qapp,
            lambda: "Waiting for the user on box" in window.statusBar().currentMessage(),
        )
        return True

    server = make_share_server(credentials, tmp_path, approve=approve)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        pump(qapp, lambda: window.viewer.has_frame)
        assert "asking its user for permission" in window.connection_log.toPlainText()
    finally:
        window.close()
        server.close()


def test_denied_connection_keeps_denied_state(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path, approve=lambda *_: False)
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        pump(qapp, lambda: window._denied)
        pump(qapp, lambda: "Denied" in window.statusBar().currentMessage())
        key = f"127.0.0.1:{server.port}"
        pump(qapp, lambda: window.inventory._peers[key].state == "denied")
        # The trailing socket disconnect must not overwrite "denied".
        pump(qapp, lambda: True, timeout=0.3)
        assert window.inventory._peers[key].state == "denied"
    finally:
        window.close()
        server.close()


def test_activating_a_second_server_replaces_the_connection(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        pump(qapp, lambda: window._connected)
        first_client = window._client
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        assert window._client is not first_client
        pump(qapp, lambda: window._connected)
    finally:
        window.close()
        server.close()


def test_forget_server_disconnects_and_forgets(qapp, credentials, tmp_path, monkeypatch):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    key = f"127.0.0.1:{server.port}"
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        pump(qapp, lambda: window._connected)
        assert window._known_servers.get(key) is not None

        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        window._forget_server(key)
        pump(qapp, lambda: not window._connected)
        assert window._known_servers.get(key) is None
        assert window.inventory._peers[key].state == "forgotten"

        # Answering "No" leaves a known server alone.
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
        )
        window._forget_server(key)
    finally:
        window.close()
        server.close()


def test_discovery_panel_lists_scan_results(qapp, monkeypatch):
    info = ServerInfo(name="box", host="10.0.0.7", port=1234)
    monkeypatch.setattr(client_module, "discover_servers", lambda: [info])
    panel = DiscoveryPanel()
    found: list[list] = []
    activated: list[ServerInfo] = []
    panel.serversFound.connect(found.append)
    panel.serverActivated.connect(activated.append)
    panel.refresh()
    assert not panel._refresh_button.isEnabled()
    pump(qapp, lambda: found)
    assert found == [[info]]
    assert panel._refresh_button.isEnabled()
    assert panel.server_list.count() == 1

    item = panel.server_list.item(0)
    assert isinstance(item, QListWidgetItem)
    panel._on_item_activated(item)
    assert activated == [info]
    assert item.data(Qt.ItemDataRole.UserRole) == info


def test_discovery_panel_survives_scan_failure(qapp, monkeypatch):
    def boom():
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(client_module, "discover_servers", boom)
    panel = DiscoveryPanel()
    found: list[list] = []
    panel.serversFound.connect(found.append)
    panel.refresh()
    pump(qapp, lambda: found)
    assert found == [[]]
    assert panel._refresh_button.isEnabled()
