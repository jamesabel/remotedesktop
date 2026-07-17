"""Client GUI application: discovers servers on the LAN, connects to one,
and shows its desktop in a viewer widget."""

import sys
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from remotedesktop.discovery import ServerInfo, discover_servers
from remotedesktop.viewer import ViewerWidget


class DiscoveryPanel(QWidget):
    """Scans the LAN for servers and lists them for the user to pick."""

    serverActivated = Signal(ServerInfo)
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

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        self.serverActivated.emit(item.data(Qt.ItemDataRole.UserRole))


class ClientWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Client")
        self.viewer = ViewerWidget(self)
        self.setCentralWidget(self.viewer)
        self.discovery_panel = DiscoveryPanel(self)
        dock = QDockWidget("Servers", self)
        dock.setWidget(self.discovery_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.discovery_panel.serverActivated.connect(self._on_server_activated)

    def _on_server_activated(self, server: ServerInfo) -> None:
        self.statusBar().showMessage(
            f"Connecting to {server.name} ({server.host}:{server.port}) is not implemented yet"
        )


def main() -> None:
    app = QApplication(sys.argv)
    window = ClientWindow()
    window.show()
    window.discovery_panel.refresh()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
