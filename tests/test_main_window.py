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
    db_name="app.db", discovery_port=None, reconnect_base_seconds=2.0,
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
        reconnect_base_seconds=reconnect_base_seconds,
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
        for expected in ("Server", "Connections", "Performance", "Preferences", "About"):
            assert expected in labels
        assert "Connection log" not in labels  # folded into Connections
        # With no connection, tab 0 is the "Server" instructions placeholder,
        # and it is the landing tab.
        assert labels[0] == "Server"
        assert tabs.currentIndex() == 0
        # The Connections tab grids sharing status, both peer inventories,
        # and the connection log.
        from PySide6.QtWidgets import QGroupBox

        # Left column: this computer (sharing, log); right: the LAN tables.
        connections_tab = tabs.widget(labels.index("Connections"))
        groups = [g.title() for g in connections_tab.findChildren(QGroupBox)]
        assert groups == [
            "Sharing this computer",
            "Connection log",
            "Servers on LAN",
            "Clients on LAN",
        ]
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
        window.sharing_tab.set_mode("off")
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
        window.sharing_tab.set_mode("off")
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


def _menu_actions(window, title):
    # Never QAction.menu() here: PySide6 hands that wrapper to Python
    # ownership, and its garbage collection deletes the real menu.
    menus = {
        "&File": window._file_menu,
        "&View": window._view_menu,
        "&Help": window._help_menu,
    }
    return {action.text(): action for action in menus[title].actions()}


def test_menu_bar_quit_and_about(qapp, tmp_path, monkeypatch):
    window = make_window(tmp_path)
    try:
        assert [a.text() for a in window.menuBar().actions()] == ["&File", "&View", "&Help"]
        file_actions = _menu_actions(window, "&File")
        assert "&Restart app…" in file_actions

        _menu_actions(window, "&Help")["&About"].trigger()
        tabs = window.centralWidget()
        assert tabs.tabText(tabs.currentIndex()) == "About"

        quits = []
        monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
        file_actions["&Quit"].trigger()
        assert quits == [True]
    finally:
        window.close()


def test_close_tab_action_closes_only_session_tabs(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    try:
        # A fixed tab is current at startup: the action is disabled and inert.
        assert not window.close_tab_action.isEnabled()
        window.close_tab_action.trigger()
        assert window._sessions == []

        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        pump(qapp, lambda: window._sessions[0].connected)
        assert window.close_tab_action.isEnabled()  # session tab became current
        window.close_tab_action.trigger()
        assert window._sessions == []
        assert not window.close_tab_action.isEnabled()
    finally:
        window.close()
        server.close()


def test_servers_dock_can_be_reopened_and_layout_persists(qapp, tmp_path):
    window = make_window(tmp_path)
    try:
        window.show()
        assert window.servers_dock.isVisible()
        window.servers_dock.close()
        assert not window.servers_dock.isVisible()
        # The View menu toggle is the way back.
        _menu_actions(window, "&View")["&Servers panel"].trigger()
        assert window.servers_dock.isVisible()
        # Close it again; the layout persists to the next start (same DB).
        window.servers_dock.close()
    finally:
        window.close()

    reopened = make_window(tmp_path)
    try:
        reopened.show()
        assert not reopened.servers_dock.isVisible()
    finally:
        reopened.close()


def test_refresh_action_triggers_a_scan(qapp, tmp_path, monkeypatch):
    scans = []
    monkeypatch.setattr(client_module, "discover_servers", lambda: scans.append(True) or [])
    window = make_window(tmp_path)
    try:
        _menu_actions(window, "&View")["&Refresh server list"].trigger()
        pump(qapp, lambda: scans)
        assert scans == [True]
    finally:
        window.close()


def test_actual_size_action_applies_per_session(qapp, credentials, tmp_path):
    server_a = make_share_server(credentials, tmp_path, db_name="server_a.db")
    server_b = make_share_server(credentials, tmp_path, db_name="server_b.db")
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="alpha", host="127.0.0.1", port=server_a.port))
        window._on_server_activated(ServerInfo(name="beta", host="127.0.0.1", port=server_b.port))
        first, second = window._sessions
        pump(qapp, lambda: first.viewer.has_frame and second.viewer.has_frame)

        # Second session'"'"'s tab is current; switch it to actual size.
        assert window.actual_size_action.isEnabled()
        window.actual_size_action.trigger()  # toggles checked -> True
        assert second.actual_size
        assert not second.page.widgetResizable()
        assert not first.actual_size

        # The action follows the current tab'"'"'s session state.
        tabs = window.centralWidget()
        tabs.setCurrentWidget(first.page)
        assert not window.actual_size_action.isChecked()
        tabs.setCurrentWidget(second.page)
        assert window.actual_size_action.isChecked()
        # Fixed tab: disabled and unchecked.
        tabs.setCurrentIndex(2)
        assert not window.actual_size_action.isEnabled()
        assert not window.actual_size_action.isChecked()
    finally:
        window.close()
        server_a.close()
        server_b.close()


def test_fullscreen_strips_and_restores_chrome(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    try:
        window.show()
        # Not on a session tab: F11 does nothing.
        assert not window.fullscreen_action.isEnabled()
        window._toggle_fullscreen()
        assert not window.isFullScreen()

        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        window.servers_dock.close()  # user had closed the dock beforehand

        assert window.fullscreen_action.isEnabled()
        window.fullscreen_action.trigger()
        assert window.isFullScreen()
        assert not window.menuBar().isVisible()
        assert not window.statusBar().isVisible()
        assert not window.centralWidget().tabBar().isVisible()
        assert not window.servers_dock.isVisible()

        window.fullscreen_action.trigger()
        assert not window.isFullScreen()
        assert window.menuBar().isVisible()
        assert window.statusBar().isVisible()
        assert window.centralWidget().tabBar().isVisible()
        # The dock the user closed before fullscreen stays closed.
        assert not window.servers_dock.isVisible()
    finally:
        window.close()
        server.close()


def test_auto_reconnect_recovers_after_server_restart(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    port = server.port
    window = make_window(tmp_path, reconnect_base_seconds=0.05)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        assert session.auto_reconnect

        server.close()
        pump(qapp, lambda: "reconnecting" in session.status_text.lower())
        assert session.reconnect_attempts >= 1

        # The server comes back on the same port: the stored token makes the
        # reconnect promptless, and the backoff counter resets.
        replacement = ShareServer(
            approve_client=lambda *_: True,
            credentials=credentials,
            paired=PairedClients(db.connect(tmp_path / "server.db")),
        )
        assert replacement.listen(port)
        try:
            pump(qapp, lambda: session.connected, timeout=15.0)
            assert session.reconnect_attempts == 0
            pump(qapp, lambda: session.viewer.has_frame)
        finally:
            replacement.close()
    finally:
        window.close()
        server.close()


def test_auto_reconnect_backs_off_while_server_stays_down(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path, reconnect_base_seconds=0.05)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        server.close()
        # Repeated failures keep retrying with doubling delays.
        pump(qapp, lambda: session.reconnect_attempts >= 3, timeout=15.0)
        assert not session.connected
    finally:
        window.close()
        server.close()


def test_denial_stops_auto_reconnect(qapp, credentials, tmp_path, monkeypatch):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path, reconnect_base_seconds=0.05)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)

        # Revoke this window's own client identity (a per-DB UUID).
        server.revoke_client(window._identity[0])
        pump(qapp, lambda: session.denied)
        pump(qapp, lambda: True, timeout=0.5)  # give a wrong retry time to fire
        assert not session.auto_reconnect
        assert session.reconnect_attempts == 0
        assert session.reconnect_timer is None or not session.reconnect_timer.isActive()
        assert "Denied" in window.statusBar().currentMessage() or session.denied
    finally:
        window.close()
        server.close()


def test_closing_the_tab_stops_auto_reconnect(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path, reconnect_base_seconds=0.05)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        server.close()
        pump(qapp, lambda: "reconnecting" in session.status_text.lower())
        window._on_tab_close_requested(0)
        assert window._sessions == []
        assert session.reconnect_timer is None or not session.reconnect_timer.isActive()
    finally:
        window.close()
        server.close()


def test_discovery_panel_connect_button_and_selection_preservation(qapp):
    panel = DiscoveryPanel()
    activated = []
    panel.serverActivated.connect(activated.append)
    first = ServerInfo(name="A", host="10.0.0.1", port=1111)
    second = ServerInfo(name="B", host="10.0.0.2", port=2222)
    assert not panel.connect_button.isEnabled()
    assert panel.server_list.placeholder_text  # empty-state hint exists

    panel._show_results([first, second])
    panel.server_list.setCurrentRow(1)
    assert panel.connect_button.isEnabled()
    # A background rescan repopulates the list without losing the selection.
    panel._show_results([first, second])
    assert panel.selected_server() == second
    panel.connect_button.click()
    assert activated == [second]


def test_sharing_indicator_lifecycle(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    serving = make_window(tmp_path, credentials, serving=True, db_name="serving.db")
    viewing = make_window(tmp_path, db_name="viewing.db")
    try:
        assert serving._sharing_indicator.text() == "Sharing — no viewers"
        assert not serving._sharing_indicator.isHidden()
        assert viewing._sharing_indicator.isHidden()  # not sharing

        port = serving.sharing_tab.share_server.port
        viewing.discovery_panel.serverActivated.emit(
            ServerInfo(name="box", host="127.0.0.1", port=port)
        )
        session = viewing._sessions[0]
        pump(qapp, lambda: session.connected)
        pump(qapp, lambda: serving._sharing_indicator.text() == "Sharing — 1 viewer(s)")

        serving.sharing_tab.set_mode("off")
        assert serving._sharing_indicator.isHidden()
    finally:
        viewing.close()
        serving.close()


def test_quit_with_viewers_asks_for_confirmation(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    serving = make_window(tmp_path, credentials, serving=True, db_name="serving.db")
    viewing = make_window(tmp_path, db_name="viewing.db")
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))
    try:
        port = serving.sharing_tab.share_server.port
        viewing.discovery_panel.serverActivated.emit(
            ServerInfo(name="box", host="127.0.0.1", port=port)
        )
        pump(qapp, lambda: viewing._sessions and viewing._sessions[0].connected)
        pump(qapp, lambda: serving.sharing_tab.viewer_count == 1)

        # Declining keeps the app running and still sharing.
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
        )
        serving._quit()
        assert quits == []
        assert serving.sharing_tab.serving
        assert not serving._quitting

        # Accepting quits and stops sharing.
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        serving._quit()
        assert quits == [True]
        assert serving.sharing_tab.share_server is None
    finally:
        viewing.close()
        serving.close()


def test_quit_without_viewers_never_prompts(qapp, credentials, tmp_path, monkeypatch):
    serving = make_window(tmp_path, credentials, serving=True)
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(True)))

    def prompted(*_args, **_kwargs):
        raise AssertionError("must not prompt with zero viewers")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(prompted))
    serving._quit()
    assert quits == [True]


def test_preferences_sharing_mode_drives_the_sharing_lifecycle(qapp, credentials, tmp_path):
    window = make_window(tmp_path, credentials)
    try:
        assert not window.sharing_tab.serving
        assert window.preferences_tab.sharing_off_radio.isChecked()

        window.preferences_tab.sharing_view_radio.setChecked(True)
        assert window.sharing_tab.serving
        assert window.sharing_tab.share_server._input_allowed is False

        server_before = window.sharing_tab.share_server
        window.preferences_tab.sharing_control_radio.setChecked(True)
        assert window.sharing_tab.share_server is server_before  # live switch
        assert window.sharing_tab.share_server._input_allowed is True

        window.preferences_tab.sharing_off_radio.setChecked(True)
        assert not window.sharing_tab.serving
        assert window._sharing_indicator.isHidden()
    finally:
        window.close()


def test_server_placeholder_tab_swaps_with_sessions(qapp, credentials, tmp_path):
    from PySide6.QtWidgets import QLabel, QTabBar

    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    tabs = window.centralWidget()
    try:
        assert tabs.tabText(0) == "Server"
        label = tabs.widget(0).findChild(QLabel)
        assert label is not None and "No server connected" in label.text()
        # The placeholder is not closable.
        bar = tabs.tabBar()
        assert bar.tabButton(0, QTabBar.ButtonPosition.RightSide) is None
        assert bar.tabButton(0, QTabBar.ButtonPosition.LeftSide) is None

        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        session = window._sessions[0]
        pump(qapp, lambda: session.connected)
        # The session took the placeholder'"'"'s place: tab 0 IS the server now.
        assert tabs.indexOf(window._no_session_page) == -1
        assert tabs.tabText(0) == session.name
        assert "Server" not in [tabs.tabText(i) for i in range(tabs.count())]

        window._on_tab_close_requested(0)
        assert window._sessions == []
        assert tabs.tabText(0) == "Server"  # the instructions are back
        assert tabs.currentIndex() == 0
    finally:
        window.close()
        server.close()


def test_own_server_cannot_be_connected(qapp, credentials, tmp_path):
    window = make_window(tmp_path, credentials, serving=True)
    try:
        own_port = window.sharing_tab.share_server.port
        own = ServerInfo(name="ME", host="127.0.0.1", port=own_port)
        other = ServerInfo(name="OTHER", host="127.0.0.1", port=own_port + 1)

        # The window-level entry point refuses outright.
        window._on_server_activated(own)
        assert window._sessions == []
        assert "cannot connect to itself" in window.connection_log.toPlainText()

        # The panel disables Connect for the self row and swallows activation.
        panel = window.discovery_panel
        panel._show_results([own, other])
        activated = []
        panel.serverActivated.connect(activated.append)
        panel.server_list.setCurrentRow(0)
        assert not panel.connect_button.isEnabled()
        panel._on_item_activated(panel.server_list.item(0))
        assert activated == []
        # A normal server keeps working.
        panel.server_list.setCurrentRow(1)
        assert panel.connect_button.isEnabled()
    finally:
        window.close()

def test_discovery_panel_scans_only_at_startup(qapp, monkeypatch):
    import time

    scans = []
    monkeypatch.setattr(client_module, "discover_servers", lambda: scans.append(True) or [])
    panel = DiscoveryPanel(auto_scan=True)
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    panel.show()
    try:
        pump(qapp, lambda: len(scans) == 1)  # the single startup scan
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert len(scans) == 1  # no periodic background rescans
        panel.refresh()  # only a manual refresh scans again
        pump(qapp, lambda: len(scans) == 2)
    finally:
        panel.close()


def test_open_sessions_are_restored_on_restart(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
    pump(qapp, lambda: window._sessions[0].connected)
    window.close()

    # Same DB = same machine restarting: the connection comes back on its
    # own (the stored token makes it promptless).
    reopened = make_window(tmp_path)
    try:
        assert len(reopened._sessions) == 1
        restored = reopened._sessions[0]
        assert restored.key == f"127.0.0.1:{server.port}"
        pump(qapp, lambda: restored.connected)
        pump(qapp, lambda: restored.viewer.has_frame)
    finally:
        reopened.close()
        server.close()


def test_restored_session_keeps_trying_until_the_server_returns(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    port = server.port
    window = make_window(tmp_path)
    window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=port))
    pump(qapp, lambda: window._sessions[0].connected)
    window.close()
    server.close()  # the server is gone when the app "restarts"

    reopened = make_window(tmp_path, reconnect_base_seconds=0.05)
    try:
        restored = reopened._sessions[0]
        pump(qapp, lambda: restored.reconnect_attempts >= 1)  # backoff armed
        assert not restored.connected

        revived = ShareServer(
            approve_client=lambda *_: True,
            credentials=credentials,
            paired=PairedClients(db.connect(tmp_path / "server.db")),
        )
        assert revived.listen(port)
        try:
            pump(qapp, lambda: restored.connected, timeout=15.0)
        finally:
            revived.close()
    finally:
        reopened.close()


def test_closed_tabs_are_not_restored(qapp, credentials, tmp_path):
    server = make_share_server(credentials, tmp_path)
    window = make_window(tmp_path)
    try:
        window._on_server_activated(ServerInfo(name="box", host="127.0.0.1", port=server.port))
        pump(qapp, lambda: window._sessions[0].connected)
        window._on_tab_close_requested(0)  # the user closed it on purpose
        assert window._sessions == []
    finally:
        window.close()

    reopened = make_window(tmp_path)
    try:
        assert reopened._sessions == []
    finally:
        reopened.close()
        server.close()
