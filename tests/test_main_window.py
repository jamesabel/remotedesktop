"""Tests for the unified app window: viewing sessions, sharing, tray."""

import socket
import sys

from PySide6.QtCore import QProcess, Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem, QMessageBox

from remotedesktop import client as client_module
from remotedesktop import db
from remotedesktop.app import MainWindow
from remotedesktop.autostart import Autostart
from remotedesktop.client import DiscoveryPanel
from remotedesktop.config import PairedClients, Settings
from remotedesktop.discovery import ServerInfo
from remotedesktop.sharing import ShareServer

from test_discovery import free_udp_port
from test_sharing import pump
from test_sharing_tab import stub_approval_prompt

_TEST_AUTOSTART_KEY = r"Software\remotedesktop-tests\WindowRun"


def make_window(
    tmp_path, credentials=None, *, serving=False, tray_available=False,
    db_name="app.db", discovery_port=None,
):
    """A MainWindow on a temp DB with everything injected.

    auto_scan=False: window tests must never broadcast a discovery probe on
    the LAN. Serving instances always get injected credentials (never create
    real cert files) and ephemeral ports.
    """
    assert not serving or credentials is not None
    connection = db.connect(tmp_path / db_name)
    if serving:
        Settings(connection).set("server_enabled", "1")
    return MainWindow(
        connection=connection,
        auto_scan=False,
        credentials=credentials,
        autostart=Autostart(key_path=_TEST_AUTOSTART_KEY, value_name="main-window-test"),
        discovery_port=discovery_port if discovery_port is not None else free_udp_port(),
        connect_port=0,
        tray_available=tray_available,
    )


def make_share_server(credentials, tmp_path, *, approve=lambda *_: True, db_name="server.db"):
    server = ShareServer(
        approve_client=approve,
        credentials=credentials,
        paired=PairedClients(db.connect(tmp_path / db_name)),
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
        window._show_server_log("", "some log text")
        dialog = window.findChild(PeerLogDialog)
        assert dialog is not None
        assert "Log from server" in dialog.windowTitle()
        dialog.close()
    finally:
        window.close()


def test_window_starts_disconnected_and_not_sharing(qapp, tmp_path):
    window = make_window(tmp_path)
    try:
        assert window.statusBar().currentMessage() == "Not connected"
        assert "Remote Desktop started" in window.connection_log.toPlainText()
        tabs = window.centralWidget()
        labels = [tabs.tabText(i) for i in range(tabs.count())]
        for expected in ("Server", "Performance", "Connection log", "Preferences", "About"):
            assert expected in labels
        # The Server tab groups the sharing opt-in and both peer inventories.
        from PySide6.QtWidgets import QGroupBox

        server_tab = tabs.widget(labels.index("Server"))
        groups = [g.title() for g in server_tab.findChildren(QGroupBox)]
        assert groups == ["Sharing this computer", "Servers on LAN", "Clients on LAN"]
        assert window._sessions == []  # server tabs appear only on connection
        assert not window.sharing_tab.serving
        # Idle: neither role schedules periodic work.
        assert not window.client_performance._timer.isActive()
        assert not window.server_performance._timer.isActive()
        assert not window.windowIcon().isNull()
        from remotedesktop import __version__

        assert window.windowTitle() == f"Remote Desktop {__version__}"
    finally:
        window.close()


def test_sharing_window_title_carries_the_suffix(qapp, credentials, tmp_path):
    window = make_window(tmp_path, credentials, serving=True)
    try:
        from remotedesktop import __version__

        assert window.sharing_tab.serving
        assert window.windowTitle() == f"Remote Desktop {__version__} — sharing"
        window.sharing_tab.share_checkbox.setChecked(False)
        assert window.windowTitle() == f"Remote Desktop {__version__}"
    finally:
        window.close()


def test_discovered_servers_are_recorded(qapp, tmp_path):
    window = make_window(tmp_path)
    try:
        info = ServerInfo(name="box", host="10.0.0.7", port=1234)
        window._record_discovered([info])
        peers = window.client_inventory.peers()
        assert len(peers) == 1
        assert peers[0].state == "discovered"
    finally:
        window.close()


def test_connect_view_and_disconnect_full_flow(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    info = ServerInfo(name="box", host="127.0.0.1", port=server.port)
    try:
        window._on_server_activated(info)
        assert len(window._sessions) == 1
        session = window._sessions[0]
        pump(qapp, lambda: session.viewer.has_frame)
        assert session.connected
        assert "Viewing" in window.statusBar().currentMessage()
        # The welcome carried the server's app version for display.
        from remotedesktop import __version__

        assert f"({__version__})" in window.statusBar().currentMessage()
        # The session tab and the window title carry the server's name (the
        # hostname it reported in its welcome), so a minimized window says
        # who it is connected to.
        server_name = socket.gethostname()
        assert session.name == server_name
        assert window.centralWidget().tabText(0) == server_name
        assert window.windowTitle().startswith(f"{server_name} — ")
        key = f"127.0.0.1:{server.port}"
        assert window.client_inventory._peers[key].state == "connected"

        # Input from the viewer is forwarded while connected.
        window._on_input_event(session, {"action": "move", "x": 0.5, "y": 0.5})

        # A re-scan that finds the connected server must not downgrade it.
        window._record_discovered([info])
        assert window.client_inventory._peers[key].state == "connected"

        server.close()
        pump(qapp, lambda: not session.connected)
        assert window.client_inventory._peers[key].state == "disconnected"
        # The tab stays (ready to reconnect); the title reverts to the base.
        assert window._sessions == [session]
        assert window.windowTitle() == f"Remote Desktop {__version__}"
    finally:
        window.close()
        server.close()


def test_connects_to_multiple_servers_concurrently(qapp, credentials, tmp_path):
    server_a = make_share_server(credentials, tmp_path, db_name="server_a.db")
    server_b = make_share_server(credentials, tmp_path, db_name="server_b.db")
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="alpha", host="127.0.0.1", port=server_a.port))
        window._on_server_activated(ServerInfo(name="beta", host="127.0.0.1", port=server_b.port))
        assert len(window._sessions) == 2
        first, second = window._sessions
        pump(qapp, lambda: first.viewer.has_frame and second.viewer.has_frame)
        assert first.connected and second.connected
        # One tab per server, and the window title lists every connected name.
        tabs = window.centralWidget()
        assert tabs.tabText(0) == first.name and tabs.tabText(1) == second.name
        assert window.windowTitle().startswith(f"{first.name}, {second.name} — ")

        # Closing one session's tab disconnects only that server.
        window._on_tab_close_requested(0)
        assert window._sessions == [second]
        pump(qapp, lambda: True, timeout=0.3)  # let the aborted socket settle
        assert second.connected
        frames_before = second.frame_count
        pump(qapp, lambda: second.frame_count > frames_before)  # still streaming
    finally:
        window.close()
        server_a.close()
        server_b.close()


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
        session = window._sessions[0]
        pump(qapp, lambda: session.viewer.has_frame)
        assert "asking its user for permission" in window.connection_log.toPlainText()
    finally:
        window.close()
        server.close()


def test_denied_connection_keeps_denied_state(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path, approve=lambda *_: False)
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.denied)
        pump(qapp, lambda: "Denied" in window.statusBar().currentMessage())
        key = f"127.0.0.1:{server.port}"
        pump(qapp, lambda: window.client_inventory._peers[key].state == "denied")
        # The trailing socket disconnect must not overwrite "denied".
        pump(qapp, lambda: True, timeout=0.3)
        assert window.client_inventory._peers[key].state == "denied"
    finally:
        window.close()
        server.close()


def test_activating_a_connected_server_reuses_its_session(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    info = ServerInfo(name="box", host="127.0.0.1", port=server.port)
    try:
        window._on_server_activated(info)
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        # Activating the same server again keeps the live session and its tab.
        window._on_server_activated(info)
        assert window._sessions == [session]
        assert session.connected
        assert "Already connected" in window.connection_log.toPlainText()
    finally:
        window.close()
        server.close()


def test_reactivating_a_disconnected_server_reconnects_in_its_tab(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    info = ServerInfo(name="box", host="127.0.0.1", port=server.port)
    try:
        window._on_server_activated(info)
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        # Drop the connection from the client side; the server keeps running.
        session.client._socket.disconnectFromHost()
        pump(qapp, lambda: not session.connected)

        window._on_server_activated(info)
        # Same host:port → the existing session reconnects in its own tab.
        assert window._sessions == [session]
        pump(qapp, lambda: session.connected)
        pump(qapp, lambda: session.viewer.has_frame)
    finally:
        window.close()
        server.close()


def test_forget_server_disconnects_and_forgets(qapp, credentials, tmp_path, monkeypatch):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    key = f"127.0.0.1:{server.port}"
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        assert window._known_servers.get(key) is not None

        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        window._forget_server(key)
        assert window._sessions == []  # the session tab went with it
        assert window._known_servers.get(key) is None
        # Forgotten servers vanish from the inventory instead of lingering.
        assert key not in window.client_inventory._peers

        # Answering "No" leaves a known server alone.
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
        )
        window._forget_server(key)
    finally:
        window.close()
        server.close()


def test_version_mismatch_warns_but_still_connects(qapp, credentials, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "show", lambda self: shown.append(self.windowTitle()))
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.viewer.has_frame)
        assert not session.version_mismatch  # same build on both ends
        assert shown == []
        # Re-run the connected handler as if the server had reported a
        # different major version.
        session.client.server_app_version = "99.0.0"
        window._on_connected(session, "box")
        assert session.version_mismatch
        assert shown == ["Version mismatch"]  # strong, but non-modal
        assert "WARNING: version mismatch" in window.connection_log.toPlainText()
        assert "99.0.0 ⚠ VERSION MISMATCH" in window.statusBar().currentMessage()
        assert session.connected  # the user may still use the connection
    finally:
        window.close()
        server.close()


def test_close_while_sharing_hides_to_tray(qapp, credentials, tmp_path, monkeypatch):
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    window = make_window(tmp_path, credentials, serving=True, tray_available=True)
    try:
        assert window._tray is not None
        window.show()
        window.close()
        assert window.isHidden()  # hidden, not closed
        assert quits == []
        assert window.sharing_tab.serving  # still sharing in the background

        # Quit (the tray menu action) really exits and stops sharing.
        window._quit()
        assert quits == [True]
        assert window.sharing_tab.share_server is None
    finally:
        if not quits:
            window._quit()


def test_close_without_sharing_quits(qapp, tmp_path, monkeypatch):
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    window = make_window(tmp_path, tray_available=True)
    assert window._tray is None  # no tray while not sharing
    window.show()
    window.close()
    assert quits == [True]


def test_stopping_sharing_while_hidden_restores_the_window(qapp, credentials, tmp_path, monkeypatch):
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    window = make_window(tmp_path, credentials, serving=True, tray_available=True)
    try:
        window.show()
        window.close()
        assert window.isHidden()
        window.sharing_tab.share_checkbox.setChecked(False)
        # No tray icon without sharing, so the window must come back.
        assert not window.isHidden()
        assert window._tray is None
    finally:
        window._quit()


def test_own_server_is_labeled_in_discovery_results(qapp, credentials, tmp_path):
    window = make_window(tmp_path, credentials, serving=True)
    try:
        own_port = window.sharing_tab.share_server.port
        window.discovery_panel._show_results(
            [
                ServerInfo(name="ME", host="127.0.0.1", port=own_port),
                ServerInfo(name="OTHER", host="127.0.0.1", port=own_port + 1),
            ]
        )
        first = window.discovery_panel.server_list.item(0)
        second = window.discovery_panel.server_list.item(1)
        assert first is not None and first.text().endswith("(this computer)")
        assert second is not None and not second.text().endswith("(this computer)")
    finally:
        window.close()


def test_restart_declined_keeps_serving(qapp, credentials, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    launches = []
    monkeypatch.setattr(QProcess, "startDetached", staticmethod(lambda *a: launches.append(a)))
    window = make_window(tmp_path, credentials, serving=True)
    try:
        window._restart_app()
        assert launches == []
        assert window.sharing_tab.serving
        assert window.sharing_tab.responder is not None
    finally:
        window.close()


def test_restart_frees_ports_and_relaunches(qapp, credentials, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    launches, quits = [], []
    monkeypatch.setattr(
        QProcess, "startDetached", staticmethod(lambda *a: launches.append(a) or True)
    )
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    window = make_window(tmp_path, credentials, serving=True)
    window._restart_app()
    # Ports are freed before the new process is spawned, so it can bind them.
    assert window.sharing_tab.share_server is None
    assert window.sharing_tab.responder is None
    assert launches == [(sys.executable, ["-m", "remotedesktop"])]
    assert quits == [True]


def test_viewing_and_sharing_between_two_windows_loopback(qapp, credentials, tmp_path, monkeypatch):
    """End-to-end through two unified app instances in one process."""
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    serving = make_window(tmp_path, credentials, serving=True, db_name="serving.db")
    viewing = make_window(tmp_path, db_name="viewing.db")
    try:
        port = serving.sharing_tab.share_server.port
        viewing.discovery_panel.serverActivated.emit(
            ServerInfo(name="box", host="127.0.0.1", port=port)
        )
        session = viewing._sessions[0]
        pump(qapp, lambda: session.viewer.has_frame)
        assert session.connected
        pump(qapp, lambda: serving.sharing_tab.viewers_table.rowCount() == 1)
        # The serving side recorded the pairing in its Clients-on-LAN inventory.
        pump(
            qapp,
            lambda: any(
                p.state == "connected (paired)" for p in serving.server_inventory.peers()
            ),
        )
        # Closing the viewer's tab drops the viewer on the serving side.
        viewing._on_tab_close_requested(0)
        pump(qapp, lambda: serving.sharing_tab.viewers_table.rowCount() == 0)
        assert serving.sharing_tab.serving  # still sharing, just no viewers
    finally:
        viewing.close()
        serving.close()


def test_native_size_move_messages_drive_the_modal_pump(qapp, tmp_path):
    import ctypes
    from ctypes import wintypes

    from shiboken6 import VoidPtr

    from remotedesktop.modal_loop import WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, ModalLoopPump
    from test_modal_loop import FakeTimers

    window = make_window(tmp_path)
    try:
        timers = FakeTimers()
        window._modal_pump = ModalLoopPump(pump=lambda: None, timers=timers)
        msg = wintypes.MSG()
        msg.hWnd, msg.message = 0xBEEF, WM_ENTERSIZEMOVE
        # Qt hands nativeEvent a void*; VoidPtr is that shape from Python.
        window.nativeEvent(b"windows_generic_MSG", VoidPtr(ctypes.addressof(msg)))
        msg.message = WM_EXITSIZEMOVE
        window.nativeEvent(b"windows_generic_MSG", VoidPtr(ctypes.addressof(msg)))
        assert timers.calls == [("start", 0xBEEF), ("stop", 0xBEEF)]
    finally:
        window.close()


def test_caption_button_press_minimizes_without_native_tracking(qapp, tmp_path):
    import ctypes
    from ctypes import wintypes

    from shiboken6 import VoidPtr

    from remotedesktop.modal_loop import HTMINBUTTON, WM_NCLBUTTONDOWN, ModalLoopPump
    from test_modal_loop import FakeTimers

    window = make_window(tmp_path)
    try:
        timers = FakeTimers()
        window._modal_pump = ModalLoopPump(
            pump=lambda: None, caption_action=window._on_caption_button, timers=timers
        )
        msg = wintypes.MSG()
        msg.hWnd, msg.message, msg.wParam = 0xBEEF, WM_NCLBUTTONDOWN, HTMINBUTTON
        result = window.nativeEvent(b"windows_generic_MSG", VoidPtr(ctypes.addressof(msg)))
        assert result[0] is True  # consumed: DefWindowProc's tracking never starts
        assert timers.calls == []  # and the pump never needed to arm
        for _ in range(5):
            qapp.processEvents()  # the deferred action runs on the event loop
        assert window.isMinimized()
    finally:
        window.close()


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
