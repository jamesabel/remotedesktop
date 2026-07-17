"""Client GUI application: discovers servers on the LAN, connects to one,
and shows its desktop in a viewer widget."""

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
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import KnownServers
from remotedesktop.discovery import DISCOVERY_PORT, ServerInfo, discover_servers
from remotedesktop.inventory import ConnectionInventory, InventoryTab
from remotedesktop.sharing import ShareClient
from remotedesktop.viewer import ViewerWidget


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
        # Runs on a worker thread; the signal is delivered queued on the GUI thread.
        try:
            servers = discover_servers()
        except OSError:
            servers = []
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
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Client")
        self.viewer = ViewerWidget(self)
        self.inventory = ConnectionInventory(self)
        tabs = QTabWidget()
        tabs.addTab(self.viewer, "Remote Screen")
        tabs.addTab(InventoryTab(self.inventory), "Servers on LAN")
        self.setCentralWidget(tabs)

        self.discovery_panel = DiscoveryPanel(self)
        servers_dock = QDockWidget("Servers", self)
        servers_dock.setWidget(self.discovery_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, servers_dock)

        self.connection_log = QPlainTextEdit(self)
        self.connection_log.setReadOnly(True)
        self.connection_log.setMaximumBlockCount(1000)
        log_dock = QDockWidget("Connection log", self)
        log_dock.setWidget(self.connection_log)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, log_dock)

        self.discovery_panel.serverActivated.connect(self._on_server_activated)
        self.discovery_panel.serversFound.connect(self._record_discovered)
        self.discovery_panel.status.connect(self.log)
        self.viewer.inputEvent.connect(self._on_input_event)

        self._clipboard = ClipboardSync(parent=self)
        self._known_servers = KnownServers()
        self._client: ShareClient | None = None
        self._connected = False
        self._server_name = ""
        self._server_key = ""
        self._frame_count = 0
        self.statusBar().showMessage("Not connected")
        self.log("Client started")

    def log(self, message: str) -> None:
        self.connection_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def _record_discovered(self, servers: list) -> None:
        for server in servers:
            key = f"{server.host}:{server.port}"
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
        self.inventory.record(
            self._server_key, "attempt", name=server.name,
            address=self._server_key, detail=self._server_key,
        )
        client = ShareClient(
            known_servers=self._known_servers, clipboard=self._clipboard, parent=self
        )
        self._client = client
        client.status.connect(self.log)
        client.connected.connect(self._on_connected)
        client.denied.connect(self._on_denied)
        client.disconnected.connect(self._on_disconnected)
        client.frameReceived.connect(self._on_frame)
        self.viewer.clear(f"Connecting to {server.name} …")
        self.statusBar().showMessage(f"Connecting to {server.name} ({server.host}:{server.port}) …")
        client.connect_to(server.host, server.port)

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
        self.inventory.record(self._server_key, "denied", name=self._server_name)
        self.viewer.clear(f"Connection denied: {reason}")
        self.statusBar().showMessage(f"Denied by {self._server_name}: {reason}")

    def _on_disconnected(self) -> None:
        self._connected = False
        if self._server_key:
            self.inventory.record(self._server_key, "disconnected", name=self._server_name)
        self.viewer.clear("Disconnected")
        self.statusBar().showMessage(f"Disconnected from {self._server_name}")

    def _on_frame(self, image) -> None:
        self._frame_count += 1
        self.viewer.show_frame(image)
        self.statusBar().showMessage(
            f"Viewing {self._server_name} — {image.width()}x{image.height()} — "
            f"{self._frame_count} frames received"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._client is not None:
            self._client.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = ClientWindow()
    window.show()
    window.discovery_panel.refresh()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
