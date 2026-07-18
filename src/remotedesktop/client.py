"""Client GUI application: discovers servers on the LAN, connects to one,
and shows its desktop in a viewer widget."""

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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import __version__, db, icon, logs, window_state
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


class ClientWindow(QMainWindow):
    def __init__(
        self, *, connection: sqlite3.Connection | None = None, auto_scan: bool = True
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"Remote Desktop Client {__version__}")
        self.setWindowIcon(icon.app_icon("client"))
        # Tests inject a connection to a temp database; the app uses the default.
        self._db = connection if connection is not None else db.connect(default_db_path())
        self._settings = Settings(self._db)
        self.performance = PerformanceMonitor(
            window_seconds=float(load_performance_window_seconds(self._settings)),
            parent=self,
        )
        self.viewer = ViewerWidget(self)
        self.inventory = ConnectionInventory(self._db, "client_peers", self)
        tabs = QTabWidget()
        tabs.addTab(self.viewer, "Remote Screen")
        tabs.addTab(
            InventoryTab(self.inventory, "Forget server", self._forget_server),
            "Servers on LAN",
        )
        tabs.addTab(
            PerformanceTab(self.performance, local="client", remote="server"),
            "Performance",
        )
        self.setCentralWidget(tabs)

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
        tabs.addTab(log_tab, "Connection log")
        tabs.addTab(PreferencesTab(self._settings, self.performance), "Preferences")

        self.discovery_panel.serverActivated.connect(self._on_server_activated)
        self.discovery_panel.serversFound.connect(self._record_discovered)
        self.discovery_panel.status.connect(self.log)
        self.viewer.inputEvent.connect(self._on_input_event)

        self._clipboard = ClipboardSync(parent=self)
        self._known_servers = KnownServers(self._db)
        self._identity = load_client_identity(self._db)
        self._client: ShareClient | None = None
        self._connected = False
        self._denied = False
        self._server_name = ""
        self._server_key = ""
        self._frame_count = 0
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
        if self._connected and self._server_key == key and self._client is not None:
            self._client.close()
        self._known_servers.forget(key)
        self.inventory.record(key, "forgotten")
        self.log(f"Forgot server {key}")

    def _record_discovered(self, servers: list) -> None:
        for server in servers:
            key = f"{server.host}:{server.port}"
            if self._connected and key == self._server_key:
                continue  # don't downgrade the connected server to "discovered"
            self.inventory.record(
                key, "discovered", name=server.name, address=key, detail=key
            )

    def _on_server_activated(self, server: ServerInfo) -> None:
        if self._client is not None:
            self.log("Closing previous connection")
            self._client.close()
            self._client.deleteLater()
        self._server_name = server.name
        self._server_key = f"{server.host}:{server.port}"
        self._frame_count = 0
        self._connected = False
        self._denied = False
        self.inventory.record(
            self._server_key, "attempt", name=server.name,
            address=self._server_key, detail=self._server_key,
        )
        self.performance.reset()  # graphs show only the current connection
        client = ShareClient(
            identity=self._identity,
            known_servers=self._known_servers,
            clipboard=self._clipboard,
            performance=self.performance,
            log_provider=lambda: read_log_tail("client"),
            parent=self,
        )
        self._client = client
        client.status.connect(self.log)
        client.connected.connect(self._on_connected)
        client.approvalPending.connect(self._on_approval_pending)
        client.denied.connect(self._on_denied)
        client.disconnected.connect(self._on_disconnected)
        client.frameReceived.connect(self._on_frame)
        client.logReceived.connect(self._show_server_log)
        self.viewer.clear(f"Connecting to {server.name} …")
        self.statusBar().showMessage(f"Connecting to {server.name} ({server.host}:{server.port}) …")
        client.connect_to(server.host, server.port)

    def _on_approval_pending(self) -> None:
        self.viewer.clear(
            f"Waiting for approval — someone at {self._server_name} "
            "must allow this connection"
        )
        self.statusBar().showMessage(
            f"Waiting for the user on {self._server_name} to approve this computer …"
        )

    def _on_connected(self, server_name: str) -> None:
        self._server_name = server_name or self._server_name
        self._connected = True
        self.inventory.record(self._server_key, "connected", name=self._server_name)
        self.viewer.setFocus()
        self.statusBar().showMessage(
            f"Connected to {self._server_name} — waiting for first frame "
            "(click the view to control it)"
        )

    def _on_input_event(self, event: dict) -> None:
        if self._connected and self._client is not None:
            self._client.send_input(event)

    def _on_denied(self, reason: str) -> None:
        self._connected = False
        self._denied = True
        self.inventory.record(self._server_key, "denied", name=self._server_name)
        self.viewer.clear(f"Connection denied: {reason}")
        self.statusBar().showMessage(f"Denied by {self._server_name}: {reason}")

    def _on_disconnected(self) -> None:
        self._connected = False
        # After a denial, keep "denied" as the peer's state in the inventory
        # rather than overwriting it with the trailing "disconnected".
        if self._server_key and not self._denied:
            self.inventory.record(self._server_key, "disconnected", name=self._server_name)
        if not self._denied:
            self.viewer.clear("Disconnected")
            self.statusBar().showMessage(f"Disconnected from {self._server_name}")

    def _request_server_log(self) -> None:
        if self._client is None:
            self.log("Not connected — no server to request a log from")
            return
        self._client.request_log()

    def _show_server_log(self, text: str) -> None:
        title = (
            f'Log from server "{self._server_name}"'
            if self._server_name
            else "Log from server"
        )
        PeerLogDialog(title, text, self).show()

    def _on_frame(self, image) -> None:
        self._frame_count += 1
        self.viewer.show_frame(image)
        self.statusBar().showMessage(
            f"Viewing {self._server_name} — {image.width()}x{image.height()} — "
            f"{self._frame_count} frames received"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        window_state.save_geometry(self, self._settings, window_state.CLIENT_GEOMETRY_KEY)
        if self._client is not None:
            self._client.close()
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
