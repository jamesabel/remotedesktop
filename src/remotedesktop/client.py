"""The viewing role of the app: server discovery and per-server sessions.

`DiscoveryPanel` scans the LAN for servers; `ServerSession` bundles one
server connection (its ShareClient, its viewer tab, and the per-connection
state). The app window (`remotedesktop.app.MainWindow`) hosts the panel as
a dock and one session per connected server as a closable tab.
"""

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from remotedesktop.discovery import DISCOVERY_PORT, ServerInfo, discover_servers
from remotedesktop.sharing import ShareClient
from remotedesktop.viewer import ViewerWidget

_log = logging.getLogger("remotedesktop.client")


def _broadcast_hosts() -> tuple[str, ...]:
    """The limited broadcast plus every interface's directed broadcast.

    Windows routes 255.255.255.255 out only one interface; on a machine
    with a VPN or virtual adapters that can be the wrong one, making every
    scan come up empty. Directed subnet broadcasts (e.g. 192.168.1.255)
    reach each attached network explicitly.
    """
    hosts = {"255.255.255.255"}
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if not (flags & QNetworkInterface.InterfaceFlag.IsUp) or (
            flags & QNetworkInterface.InterfaceFlag.IsLoopBack
        ):
            continue
        for entry in interface.addressEntries():
            broadcast = entry.broadcast()
            if not broadcast.isNull() and broadcast.toString():
                hosts.add(broadcast.toString())
    return tuple(sorted(hosts))


class _ServerList(QListWidget):
    """Server list that paints a hint while empty."""

    placeholder_text = (
        "No servers found yet — turn on Screen sharing in the Preferences "
        "tab on the computer to share"
    )

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(Qt.GlobalColor.gray)
            painter.drawText(
                self.viewport().rect().adjusted(12, 12, -12, -12),
                int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
                self.placeholder_text,
            )


class DiscoveryPanel(QWidget):
    """Scans the LAN for servers and lists them for the user to pick.

    Scanning happens exactly twice as often as the user asks for it: once at
    startup (with `auto_scan`) and whenever they click Refresh (or press F5)
    — deliberately no periodic background rescans. Tests construct the panel
    with the default `auto_scan=False`, which never broadcasts on the LAN.

    `is_self` (optional) marks entries that are this very instance's own
    server — sharing and scanning in one app means you discover yourself —
    with a "(this computer)" label. Labeled, never hidden, not connectable.
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
        auto_scan: bool = False,
    ) -> None:
        super().__init__(parent)
        self._is_self = is_self
        self._scanning = False
        self._refresh_button = QPushButton("Refresh")
        self.connect_button = QPushButton("Connect")
        self.connect_button.setEnabled(False)  # needs a selection
        self.server_list = _ServerList()
        # Buttons stacked, not side by side: the dock stays narrow, leaving
        # the width to the remote screen.
        layout = QVBoxLayout(self)
        layout.addWidget(self._refresh_button)
        layout.addWidget(self.connect_button)
        layout.addWidget(self.server_list)
        self._refresh_button.clicked.connect(self.refresh)
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.server_list.itemActivated.connect(self._on_item_activated)
        self.server_list.itemSelectionChanged.connect(self._on_selection_changed)
        self.server_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.server_list.customContextMenuRequested.connect(self._on_context_menu)
        self._scanFinished.connect(self._show_results)
        if auto_scan:
            # Deferred so the host window can wire status/serversFound first.
            QTimer.singleShot(0, self.refresh)

    def refresh(self) -> None:
        if self._scanning:
            return  # a scan is already in flight (F5 while scanning)
        self._scanning = True
        self._refresh_button.setEnabled(False)
        self._refresh_button.setText("Scanning…")
        self.status.emit(f"Scanning LAN (UDP broadcast to port {DISCOVERY_PORT}) …")
        threading.Thread(target=self._scan, name="discovery-scan", daemon=True).start()

    def _scan(self) -> None:
        # Runs on a worker thread; the signal is delivered queued on the GUI
        # thread. Any failure must still emit, or the button stays disabled.
        servers: list[ServerInfo] = []
        try:
            servers = discover_servers(broadcast_hosts=_broadcast_hosts())
        except Exception:
            pass
        self._scanFinished.emit(servers)

    def selected_server(self) -> ServerInfo | None:
        item = self.server_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _show_results(self, servers: list) -> None:
        self._scanning = False
        self._refresh_button.setEnabled(True)
        self._refresh_button.setText("Refresh")
        # An auto-rescan must not wipe the user's selection out from under
        # the Connect button: reselect the same server after repopulating.
        selected = self.selected_server()
        self.server_list.clear()
        for server in servers:
            # Two lines per server — name over address — so the list (and
            # the dock) stays narrow; the LAN holds few servers, so the
            # extra height is free.
            name_line = server.name
            if self._is_self is not None and self._is_self(server):
                name_line += " (this computer)"
            item = QListWidgetItem(f"{name_line}\n{server.host}:{server.port}")
            item.setData(Qt.ItemDataRole.UserRole, server)
            self.server_list.addItem(item)
            if (
                selected is not None
                and server.host == selected.host
                and server.port == selected.port
            ):
                self.server_list.setCurrentItem(item)
        self.serversFound.emit(servers)
        found = ", ".join(f"{s.name} at {s.host}:{s.port}" for s in servers)
        self.status.emit(f"Scan finished — found: {found}" if servers else "Scan finished — no servers found")

    def _item_is_self(self, item: QListWidgetItem) -> bool:
        server = item.data(Qt.ItemDataRole.UserRole)
        return self._is_self is not None and self._is_self(server)

    def _on_selection_changed(self) -> None:
        item = self.server_list.currentItem()
        # A computer cannot connect to itself.
        self.connect_button.setEnabled(item is not None and not self._item_is_self(item))

    def _on_connect_clicked(self) -> None:
        item = self.server_list.currentItem()
        if item is not None:
            self._on_item_activated(item)

    def _on_context_menu(self, pos) -> None:
        item = self.server_list.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        connect_action = menu.addAction("Connect", lambda: self._on_item_activated(item))
        connect_action.setEnabled(not self._item_is_self(item))
        menu.exec(self.server_list.mapToGlobal(pos))

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        if self._item_is_self(item):
            self.status.emit("This computer cannot connect to itself")
            return
        self.serverActivated.emit(item.data(Qt.ItemDataRole.UserRole))


class ServerSession:
    """One server connection: its ShareClient, the viewer tab that displays
    it, and the per-connection state the window tracks for it. The session
    (and its tab) outlives a disconnect, so the user can reconnect in place;
    closing the tab ends the session."""

    def __init__(
        self,
        key: str,
        name: str,
        client: ShareClient,
        viewer: ViewerWidget,
        page: QScrollArea,
    ) -> None:
        self.key = key  # "host:port", the KnownServers/inventory key
        self.name = name
        self.client = client
        self.viewer = viewer
        self.page = page  # the tab page hosting the viewer
        self.actual_size = False  # 1:1 display instead of scaled-to-fit
        self.connected = False
        self.denied = False
        self.version_mismatch = False
        self.frame_count = 0
        # Auto-reconnect state: armed by a successful connect, driven by the
        # window's backoff timer, cancelled by denial/close/manual activation.
        self.host = ""
        self.port = 0
        self.auto_reconnect = False
        self.reconnect_attempts = 0
        self.reconnect_timer: QTimer | None = None
        # What the status bar shows while this session's tab is current.
        self.status_text = ""
