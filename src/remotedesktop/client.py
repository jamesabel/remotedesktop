"""The viewing role of the app: server discovery and per-server sessions.

`DiscoveryPanel` scans the LAN for servers; `ServerSession` bundles one
server connection (its ShareClient, its viewer tab, and the per-connection
state). The app window (`remotedesktop.app.MainWindow`) hosts the panel as
a dock and one session per connected server as a closable tab.
"""

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from remotedesktop.discovery import DISCOVERY_PORT, ServerInfo, discover_servers
from remotedesktop.sharing import ShareClient
from remotedesktop.viewer import ViewerWidget

_log = logging.getLogger("remotedesktop.client")


class DiscoveryPanel(QWidget):
    """Scans the LAN for servers and lists them for the user to pick.

    `is_self` (optional) marks entries that are this very instance's own
    server — sharing and scanning in one app means you discover yourself —
    with a "(this computer)" label. Labeled, never hidden.
    """

    serverActivated = Signal(ServerInfo)
    serversFound = Signal(list)
    status = Signal(str)
    _scanFinished = Signal(list)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        is_self: Callable[[ServerInfo], bool] | None = None,
    ) -> None:
        super().__init__(parent)
        self._is_self = is_self
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
            label = f"{server.name} ({server.host}:{server.port})"
            if self._is_self is not None and self._is_self(server):
                label += " (this computer)"
            item = QListWidgetItem(label)
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
