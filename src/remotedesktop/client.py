"""Client GUI application: discovers servers on the LAN, connects to one or
more of them, and shows each server's desktop in its own viewer tab."""

import logging
import sqlite3
import sys
import threading
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import __version__, compat, db, icon, logs, window_state
from remotedesktop.about import AboutTab
from remotedesktop.logs import PeerLogDialog, read_log_tail
from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import KnownServers, Settings, default_db_path, load_client_identity
from remotedesktop.discovery import DISCOVERY_PORT, ServerInfo, discover_servers
from remotedesktop.inventory import ConnectionInventory, InventoryTab
from remotedesktop.performance import PerformanceMonitor, PerformanceTab
from remotedesktop.preferences import PreferencesTab, load_performance_window_seconds
from remotedesktop.sharing import ShareClient
from remotedesktop.viewer import ViewerWidget

_log = logging.getLogger("remotedesktop.client")


class DiscoveryPanel(QWidget):
    """Scans the LAN for servers and lists them for the user to pick."""

    serverActivated = Signal(ServerInfo)
    serversFound = Signal(list)
    status = Signal(str)
    _scanFinished = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._refresh_button = QPushButton("Refresh")
        self.server_list = QListWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self._refresh_button)
        layout.addWidget(self.server_list)
        self._refresh_button.clicked.connect(self.refresh)
        self.server_list.itemActivated.connect(self._on_item_activated)
        self._scanFinished.connect(self._show_results)

    def refresh(self) -> None:
        self._refresh_button.setEnabled(False)
        self._refresh_button.setText("Scanning…")
        self.status.emit(f"Scanning LAN (UDP broadcast to port {DISCOVERY_PORT}) …")
        threading.Thread(target=self._scan, name="discovery-scan", daemon=True).start()

    def _scan(self) -> None:
        # Runs on a worker thread; the signal is delivered queued on the GUI
        # thread. Any failure must still emit, or the button stays disabled.
        servers: list[ServerInfo] = []
        try:
            servers = discover_servers()
        except Exception:
            pass
        self._scanFinished.emit(servers)

    def _show_results(self, servers: list) -> None:
        self._refresh_button.setEnabled(True)
        self._refresh_button.setText("Refresh")
        self.server_list.clear()
        for server in servers:
            item = QListWidgetItem(f"{server.name} ({server.host}:{server.port})")
            item.setData(Qt.ItemDataRole.UserRole, server)
            self.server_list.addItem(item)
        self.serversFound.emit(servers)
        found = ", ".join(f"{s.name} at {s.host}:{s.port}" for s in servers)
        self.status.emit(f"Scan finished — found: {found}" if servers else "Scan finished — no servers found")

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        self.serverActivated.emit(item.data(Qt.ItemDataRole.UserRole))


class ServerSession:
    """One server connection: its ShareClient, the viewer tab that displays
    it, and the per-connection state the window tracks for it. The session
    (and its tab) outlives a disconnect, so the user can reconnect in place;
    closing the tab ends the session."""

    def __init__(self, key: str, name: str, client: ShareClient, viewer: ViewerWidget) -> None:
        self.key = key  # "host:port", the KnownServers/inventory key
        self.name = name
        self.client = client
        self.viewer = viewer
        self.connected = False
        self.denied = False
        self.version_mismatch = False
        self.frame_count = 0
        # What the status bar shows while this session's tab is current.
        self.status_text = ""


class ClientWindow(QMainWindow):
    def __init__(
        self, *, connection: sqlite3.Connection | None = None, auto_scan: bool = True
    ) -> None:
        super().__init__()
        self.setWindowIcon(icon.app_icon("client"))
        # Tests inject a connection to a temp database; the app uses the default.
        self._db = connection if connection is not None else db.connect(default_db_path())
        self._settings = Settings(self._db)
        self.performance = PerformanceMonitor(
            window_seconds=float(load_performance_window_seconds(self._settings)),
            parent=self,
        )
        self.inventory = ConnectionInventory(self._db, "client_peers", self)
        # One tab per server connection (inserted at the front, closable),
        # followed by the fixed tabs, which never get a close button.
        self._sessions: list[ServerSession] = []
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.currentChanged.connect(lambda _index: self._refresh_status_bar())
        self._tabs.addTab(
            InventoryTab(self.inventory, "Forget server", self._forget_server),
            "Servers on LAN",
        )
        self._tabs.addTab(
            PerformanceTab(self.performance, local="client", remote="server"),
            "Performance",
        )
        self.setCentralWidget(self._tabs)

        self.discovery_panel = DiscoveryPanel(self)
        servers_dock = QDockWidget("Servers", self)
        servers_dock.setWidget(self.discovery_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, servers_dock)

        self.connection_log = QPlainTextEdit(self)
        self.connection_log.setReadOnly(True)
        self.connection_log.setMaximumBlockCount(1000)
        self.get_log_button = QPushButton("Get server log")
        self.get_log_button.setToolTip("Ask the connected server to send its debug log")
        self.get_log_button.clicked.connect(self._request_server_log)
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.addWidget(self.get_log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        log_layout.addWidget(self.connection_log)
        self._tabs.addTab(log_tab, "Connection log")
        self._tabs.addTab(PreferencesTab(self._settings, self.performance), "Preferences")
        self._tabs.addTab(AboutTab(), "About")
        # Only session tabs are closable; strip the buttons the fixed tabs
        # got from setTabsClosable (styles place them on either side).
        bar = self._tabs.tabBar()
        for index in range(self._tabs.count()):
            for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
                bar.setTabButton(index, side, None)

        self.discovery_panel.serverActivated.connect(self._on_server_activated)
        self.discovery_panel.serversFound.connect(self._record_discovered)
        self.discovery_panel.status.connect(self.log)

        self._clipboard = ClipboardSync(parent=self)
        self._known_servers = KnownServers(self._db)
        self._identity = load_client_identity(self._db)
        self._update_window_title()
        self.statusBar().showMessage("Not connected")
        window_state.restore_geometry(self, self._settings, window_state.CLIENT_GEOMETRY_KEY)
        self.log("Client started")
        # Tests pass auto_scan=False so window tests never broadcast on the LAN.
        if auto_scan:
            self.discovery_panel.refresh()

    def log(self, message: str) -> None:
        # Everything shown in the Connection log pane also goes to the debug
        # log file (when main() enabled it), so it survives the window.
        _log.info(message)
        self.connection_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def _update_window_title(self) -> None:
        """Connected server names lead the title, so the taskbar (and a
        minimized window) says who this client is viewing."""
        names = [session.name for session in self._sessions if session.connected]
        base = f"Remote Desktop Client {__version__}"
        self.setWindowTitle(f"{', '.join(names)} — {base}" if names else base)

    def _session_for_key(self, key: str) -> ServerSession | None:
        for session in self._sessions:
            if session.key == key:
                return session
        return None

    def _session_for_viewer(self, widget) -> ServerSession | None:
        for session in self._sessions:
            if session.viewer is widget:
                return session
        return None

    def _set_session_status(self, session: ServerSession, text: str) -> None:
        session.status_text = text
        if self._tabs.currentWidget() is session.viewer:
            self.statusBar().showMessage(text)

    def _refresh_status_bar(self) -> None:
        session = self._session_for_viewer(self._tabs.currentWidget())
        if session is not None:
            self.statusBar().showMessage(session.status_text)
            return
        names = [s.name for s in self._sessions if s.connected]
        self.statusBar().showMessage(
            f"Connected to {', '.join(names)}" if names else "Not connected"
        )

    def _forget_server(self, key: str) -> None:
        answer = QMessageBox.question(
            self,
            "Forget server",
            f"Forget server {key}?\n\n"
            "If connected it will be disconnected, and the next connection will "
            "need the server user to approve this computer again.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        session = self._session_for_key(key)
        if session is not None:
            self._close_session(session)
        self._known_servers.forget(key)
        self.inventory.record(key, "forgotten")
        self.log(f"Forgot server {key}")

    def _record_discovered(self, servers: list) -> None:
        for server in servers:
            key = f"{server.host}:{server.port}"
            session = self._session_for_key(key)
            if session is not None and session.connected:
                continue  # don't downgrade a connected server to "discovered"
            self.inventory.record(
                key, "discovered", name=server.name, address=key, detail=key
            )

    def _on_server_activated(self, server: ServerInfo) -> None:
        key = f"{server.host}:{server.port}"
        session = self._session_for_key(key)
        if session is not None and session.connected:
            self._tabs.setCurrentWidget(session.viewer)
            self.log(f"Already connected to {session.name} ({key})")
            return
        if session is None:
            session = self._create_session(key, server.name)
        else:
            session.name = server.name
            self._tabs.setTabText(self._sessions.index(session), session.name)
        self._connect_session(session, server.host, server.port)

    def _create_session(self, key: str, name: str) -> ServerSession:
        viewer = ViewerWidget()
        client = ShareClient(
            identity=self._identity,
            known_servers=self._known_servers,
            clipboard=self._clipboard,
            performance=self.performance,
            log_provider=lambda: read_log_tail("client"),
            parent=self,
        )
        session = ServerSession(key, name, client, viewer)
        viewer.inputEvent.connect(lambda event, s=session: self._on_input_event(s, event))
        client.status.connect(lambda message, s=session: self.log(f"[{s.name}] {message}"))
        client.connected.connect(lambda server_name, s=session: self._on_connected(s, server_name))
        client.approvalPending.connect(lambda s=session: self._on_approval_pending(s))
        client.denied.connect(lambda reason, s=session: self._on_denied(s, reason))
        client.disconnected.connect(lambda s=session: self._on_disconnected(s))
        client.frameReceived.connect(lambda image, s=session: self._on_frame(s, image))
        client.logReceived.connect(lambda text, s=session: self._show_server_log(s.name, text))
        self._sessions.append(session)
        self._tabs.insertTab(len(self._sessions) - 1, viewer, session.name)
        return session

    def _connect_session(self, session: ServerSession, host: str, port: int) -> None:
        session.frame_count = 0
        session.connected = False
        session.denied = False
        session.version_mismatch = False
        self.inventory.record(
            session.key, "attempt", name=session.name,
            address=session.key, detail=session.key,
        )
        # Graphs show only the current connection(s): clear them for a fresh
        # start, but never while another session's stream is being sampled.
        if not any(s.connected for s in self._sessions if s is not session):
            self.performance.reset()
        session.viewer.clear(f"Connecting to {session.name} …")
        self._tabs.setCurrentWidget(session.viewer)
        self._set_session_status(
            session, f"Connecting to {session.name} ({session.key}) …"
        )
        session.client.connect_to(host, port)

    def _close_session(self, session: ServerSession) -> None:
        """Disconnect the session and remove its tab (tab close / forget)."""
        session.client.close()  # a synchronous disconnect signal may fire here
        index = self._sessions.index(session)
        self._sessions.pop(index)
        self._tabs.removeTab(index)
        if session.connected and not session.denied:
            self.inventory.record(session.key, "disconnected", name=session.name)
        session.connected = False
        session.client.deleteLater()
        session.viewer.deleteLater()
        self.log(f"Closed connection to {session.name} ({session.key})")
        self._update_window_title()
        self._refresh_status_bar()

    def _on_tab_close_requested(self, index: int) -> None:
        if index < len(self._sessions):  # fixed tabs carry no close button
            self._close_session(self._sessions[index])

    def _on_approval_pending(self, session: ServerSession) -> None:
        if session not in self._sessions:
            return
        session.viewer.clear(
            f"Waiting for approval — someone at {session.name} "
            "must allow this connection"
        )
        self._set_session_status(
            session,
            f"Waiting for the user on {session.name} to approve this computer …",
        )

    def _server_label(self, session: ServerSession) -> str:
        """The server's name with its app version when it reported one,
        e.g. 'DEN-PC (0.19.0)' — flagged when its major version differs."""
        version = session.client.server_app_version
        if not version:
            return session.name
        marker = " ⚠ VERSION MISMATCH" if session.version_mismatch else ""
        return f"{session.name} ({version}{marker})"

    def _on_connected(self, session: ServerSession, server_name: str) -> None:
        if session not in self._sessions:
            return
        session.name = server_name or session.name
        self._tabs.setTabText(self._sessions.index(session), session.name)
        session.connected = True
        # Semver policy: matching majors are the compatibility contract. A
        # mismatch warns loudly (log, dialog, status bar) but never blocks —
        # the user may still try, with no guarantees.
        warning = compat.mismatch_warning(
            __version__, session.client.server_app_version, "server"
        )
        session.version_mismatch = warning is not None
        if warning:
            self.log(warning)
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                "Version mismatch",
                warning,
                QMessageBox.StandardButton.Ok,
                self,
            )
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            box.show()  # non-modal: streaming continues behind it
        self.inventory.record(session.key, "connected", name=session.name)
        session.viewer.setFocus()
        self._set_session_status(
            session,
            f"Connected to {self._server_label(session)} — waiting for first frame "
            "(click the view to control it)",
        )
        self._update_window_title()

    def _on_input_event(self, session: ServerSession, event: dict) -> None:
        if session.connected:
            session.client.send_input(event)

    def _on_denied(self, session: ServerSession, reason: str) -> None:
        if session not in self._sessions:
            return
        session.connected = False
        session.denied = True
        self.inventory.record(session.key, "denied", name=session.name)
        session.viewer.clear(f"Connection denied: {reason}")
        self._set_session_status(session, f"Denied by {session.name}: {reason}")
        self._update_window_title()

    def _on_disconnected(self, session: ServerSession) -> None:
        if session not in self._sessions:
            return
        session.connected = False
        # After a denial, keep "denied" as the peer's state in the inventory
        # rather than overwriting it with the trailing "disconnected".
        if not session.denied:
            self.inventory.record(session.key, "disconnected", name=session.name)
            session.viewer.clear("Disconnected")
            self._set_session_status(session, f"Disconnected from {session.name}")
        self._update_window_title()

    def _request_server_log(self) -> None:
        session = self._session_for_viewer(self._tabs.currentWidget())
        if session is None:
            connected = [s for s in self._sessions if s.connected]
            session = connected[-1] if connected else None
        if session is None:
            self.log("Not connected — no server to request a log from")
            return
        session.client.request_log()

    def _show_server_log(self, server_name: str, text: str) -> None:
        title = (
            f'Log from server "{server_name}"' if server_name else "Log from server"
        )
        PeerLogDialog(title, text, self).show()

    def _on_frame(self, session: ServerSession, image) -> None:
        if session not in self._sessions:
            return
        session.frame_count += 1
        session.viewer.show_frame(image)
        self._set_session_status(
            session,
            f"Viewing {self._server_label(session)} — {image.width()}x{image.height()} — "
            f"{session.frame_count} frames received",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        window_state.save_geometry(self, self._settings, window_state.CLIENT_GEOMETRY_KEY)
        for session in self._sessions:
            session.client.close()
        super().closeEvent(event)


def main() -> None:  # pragma: no cover - runs the Qt event loop
    log_path = logs.init_logging("client")
    icon.set_windows_app_id("remotedesktop.client")
    app = QApplication(sys.argv)
    app.setWindowIcon(icon.app_icon("client"))
    window = ClientWindow()  # auto_scan starts the first LAN scan
    window.log(f"Detailed log: {log_path}")
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":  # pragma: no cover
    main()
