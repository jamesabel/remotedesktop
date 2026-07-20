"""The serving role of the app: the Sharing tab.

`SharingTab` is where an instance opts in to being a server: a checkbox
starts/stops sharing this computer's screen on the LAN. It owns the
`ShareServer` and `DiscoveryResponder` lifecycle — both exist only while
sharing is enabled — and shows who is viewing (`ViewersTable`) and the app
restart button. The opt-in persists in the settings table
(`server_enabled`), so an instance that shared keeps sharing on the next
start.
"""

import logging
import socket
import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import __version__, compat, tls
from remotedesktop.config import PairedClients, Settings, default_config_dir
from remotedesktop.cursor_shape import current_cursor_shape
from remotedesktop.discovery import (
    DEFAULT_CONNECT_PORT,
    DISCOVERY_PORT,
    DiscoveryResponder,
)
from remotedesktop.logs import PeerLogDialog, read_log_tail
from remotedesktop.performance import PerformanceMonitor, format_ms, format_rate
from remotedesktop.sharing import ShareServer

_log = logging.getLogger("remotedesktop.server")

# The three-state sharing mode, persisted as two settings keys (kept from
# the two-checkbox era so existing installs migrate without ceremony).
SHARING_MODE_OFF = "off"
SHARING_MODE_VIEW = "view"  # viewers can watch only
SHARING_MODE_CONTROL = "control"  # viewers can watch and control
ALLOW_INPUT_KEY = "allow_remote_input"


def load_sharing_mode(settings: Settings) -> str:
    if not settings.get_bool("server_enabled", False):
        return SHARING_MODE_OFF
    if settings.get_bool(ALLOW_INPUT_KEY, True):
        return SHARING_MODE_CONTROL
    return SHARING_MODE_VIEW


class ViewersTable(QTableWidget):
    """Connected viewers with who/what they are and live per-viewer metrics.

    Identity fields come from each viewer's hello (`ShareServer.viewers()`);
    Send/Receive/Round trip come from the performance monitor's per-stream
    numbers. The table follows whatever ShareServer is currently set via
    `set_share_server` (None while sharing is off → empty). Rows refresh
    when the viewer count changes and once per monitor tick — but only
    while visible (background tabs schedule no work).
    """

    # No "Computer" column: the display name defaults to (and, with no UI
    # to change it, always is) the hostname the hello also reports.
    _COLUMNS = [
        "Name", "Address", "User", "OS", "Version", "Send", "Receive",
        "RTT", "RTT mean", "RTT min", "RTT max", "RTT p99", "RTT jitter",
    ]
    # Metric cells change text every tick, so their columns get a constant
    # width (sized to the widest plausible value) instead of
    # ResizeToContents — otherwise the columns visibly jitter each second.
    _RATE_COLUMNS = (5, 6)  # Send / Receive
    _MS_COLUMNS = (7, 8, 9, 10, 11, 12)  # RTT latest + window statistics
    _METRIC_COLUMNS = _RATE_COLUMNS + _MS_COLUMNS

    def __init__(self, performance: PerformanceMonitor, parent=None) -> None:
        # One extra headerless column takes the stretch so the data columns
        # stay sized to their contents (the InventoryTab spacer pattern).
        super().__init__(0, len(self._COLUMNS) + 1, parent)
        self.setHorizontalHeaderLabels(self._COLUMNS + [""])
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        # Sized to realistic maxima ("999.9 MB/s" saturates a LAN; RTTs over
        # a second are already hopeless) with slim padding — the metric
        # columns don't deserve more width than the identity ones.
        rate_width = self.fontMetrics().horizontalAdvance("999.9 MB/s") + 12
        ms_width = self.fontMetrics().horizontalAdvance("999.9 ms") + 12
        for column in self._METRIC_COLUMNS:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(column, rate_width if column in self._RATE_COLUMNS else ms_width)
        self._share_server = None
        self._performance = performance
        performance.updated.connect(self._on_monitor_tick)
        self.refresh()

    def set_share_server(self, share_server) -> None:
        """Follow a new ShareServer (or None while sharing is off)."""
        if self._share_server is not None:
            self._share_server.clientCountChanged.disconnect(self._on_count_changed)
        self._share_server = share_server
        if share_server is not None:
            share_server.clientCountChanged.connect(self._on_count_changed)
        self.refresh()

    def _on_count_changed(self, _count: int) -> None:
        self.refresh()

    def _on_monitor_tick(self) -> None:
        if self.isVisible():
            self.refresh()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh()  # catch up on metrics accrued while hidden

    @staticmethod
    def _version_cell(version: str) -> str:
        """The viewer's version, flagged when its semver major differs from
        ours (compatibility is only guaranteed within a major)."""
        if not version:
            return "—"
        if compat.mismatch_warning(__version__, version, "client") is not None:
            return f"{version} ⚠"
        return version

    def _identity_values(self, viewer: dict) -> list[str]:
        """The non-metric cells for one peer (subclasses reshape these)."""
        return [
            viewer["name"] or "(unknown)",
            viewer["address"],
            viewer["user"] or "—",
            viewer["os"] or "—",
            self._version_cell(viewer["app_version"]),
        ]

    def refresh(self) -> None:
        viewers = self._share_server.viewers() if self._share_server is not None else []
        self.setRowCount(len(viewers))
        for row, viewer in enumerate(viewers):
            metrics = self._performance.metrics_for(viewer["stream"])
            send, recv, rtt = metrics["send_bps"], metrics["recv_bps"], metrics["rtt_ms"]
            stats = metrics["rtt_stats"] or {}
            values = self._identity_values(viewer) + [
                format_rate(send) if send is not None else "—",
                format_rate(recv) if recv is not None else "—",
                format_ms(rtt) if rtt is not None else "—",
            ] + [
                format_ms(stats[key]) if key in stats else "—"
                for key in ("mean", "min", "max", "p99", "jitter")
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in self._METRIC_COLUMNS:
                    # Right-aligned numbers change magnitude without the
                    # digits appearing to wander.
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.setItem(row, column, item)


class SharingTab(QWidget):
    """The sharing role's status: summary and connected-viewers table.

    Owns the ShareServer + DiscoveryResponder: both are created when sharing
    is enabled and torn down when it is disabled, so a viewing-only instance
    binds no ports (and never creates TLS credentials). The sharing mode
    itself is a three-state choice (`set_mode`: off / view / control) driven
    by the Preferences tab. The window hosting this tab wires
    `statusMessage` to its Connection log, `peerEvent` to the server-side
    inventory, and `sharingChanged` to its tray/title state.
    """

    statusMessage = Signal(str)
    peerEvent = Signal(dict)  # {key, event, name, address, detail} for the inventory
    sharingChanged = Signal(bool)  # emitted with `serving` after start/stop
    viewerCountChanged = Signal(int)  # 0 while not serving

    def __init__(
        self,
        *,
        settings: Settings,
        connection: sqlite3.Connection,
        performance: PerformanceMonitor,
        clipboard=None,
        credentials: tuple | None = None,
        discovery_port: int = DISCOVERY_PORT,
        connect_port: int = DEFAULT_CONNECT_PORT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._connection = connection
        self._performance = performance
        self._clipboard = clipboard
        self._credentials = credentials
        self._discovery_port = discovery_port
        self._connect_port = connect_port
        self._paired = PairedClients(connection)
        self._name = socket.gethostname()

        self.share_server: ShareServer | None = None
        self.responder: DiscoveryResponder | None = None
        self._listening = False
        self._discoverable = False

        self._summary = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.viewers_table = ViewersTable(performance)
        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addWidget(self.viewers_table, stretch=1)
        self._update_summary(0)

    def restore_sharing(self) -> None:
        """Start sharing if the persisted mode is on (call after wiring)."""
        if load_sharing_mode(self._settings) != SHARING_MODE_OFF and self.share_server is None:
            self.start_sharing()

    @property
    def serving(self) -> bool:
        """True while sharing is enabled and the server is actually listening."""
        return self.share_server is not None and self._listening

    @property
    def viewer_count(self) -> int:
        return self.share_server.client_count if self.share_server is not None else 0

    @property
    def mode(self) -> str:
        return load_sharing_mode(self._settings)

    def set_mode(self, mode: str) -> None:
        """Apply a sharing mode: SHARING_MODE_OFF / _VIEW / _CONTROL.

        View ↔ control switches apply live without dropping viewers.
        """
        if mode == SHARING_MODE_OFF:
            if self.share_server is not None:
                self.stop_sharing()
            else:
                self._settings.set_bool("server_enabled", False)
            return
        allowed = mode == SHARING_MODE_CONTROL
        self._settings.set_bool(ALLOW_INPUT_KEY, allowed)
        if self.share_server is not None:
            self.share_server.set_input_allowed(allowed)  # emits its status line
            return
        self.start_sharing()

    def start_sharing(self) -> None:
        if self.share_server is not None:
            return
        # TLS credentials are created on first enable, not at construction:
        # a viewing-only instance never writes server_cert.pem.
        if self._credentials is None:
            config_dir = default_config_dir()
            self._credentials = tls.load_or_create_credentials(
                config_dir / "server_cert.pem", config_dir / "server_key.pem"
            )
        # A fresh ShareServer per enable: close() leaves teardown state
        # behind, so recreating is provably clean.
        server = ShareServer(
            self._ask_approval,
            credentials=self._credentials,
            paired=self._paired,
            clipboard=self._clipboard,
            cursor_probe=current_cursor_shape,
            performance=self._performance,
            log_provider=lambda: read_log_tail("remotedesktop"),
            input_allowed=self._settings.get_bool(ALLOW_INPUT_KEY, True),
            parent=self,
        )
        server.status.connect(self.statusMessage)
        server.clientCountChanged.connect(self._update_summary)
        server.peerEvent.connect(self.peerEvent)
        server.logReceived.connect(self._show_client_log)
        self.share_server = server
        self.viewers_table.set_share_server(server)
        self._listening = server.listen(self._connect_port)
        self._discoverable = False
        if self._listening:
            responder = DiscoveryResponder(
                self._name, server.port, discovery_port=self._discovery_port
            )
            try:
                responder.start()
            except OSError as error:
                self.statusMessage.emit(
                    f"Discovery unavailable (UDP port {self._discovery_port}): {error} — "
                    "another server may already be running"
                )
            else:
                self.responder = responder
                self._discoverable = True
                self.statusMessage.emit(
                    f'Discoverable as "{self._name}" '
                    f"(UDP port {self._discovery_port}, TCP port {server.port})"
                )
        self._settings.set_bool("server_enabled", True)
        self._update_summary(0)
        self.sharingChanged.emit(self.serving)

    def stop_sharing(self) -> None:
        self._teardown()
        self._settings.set_bool("server_enabled", False)
        self._update_summary(0)
        self.sharingChanged.emit(False)

    def shutdown(self) -> None:
        """App close: free the ports without touching the persisted opt-in."""
        self._teardown()

    def _teardown(self) -> None:
        if self.responder is not None:
            self.responder.stop()
            self.responder = None
        if self.share_server is not None:
            self.share_server.close()  # disconnects all viewers
            self.share_server.deleteLater()
            self.share_server = None
            self.viewers_table.set_share_server(None)
        self._listening = False
        self._discoverable = False

    def _update_summary(self, client_count: int) -> None:
        self.viewerCountChanged.emit(client_count if self.serving else 0)
        if self.share_server is None:
            self._summary.setText(
                "Not sharing this computer's screen — enable Server (sharing) "
                "in the Preferences tab to let others view this computer"
            )
            return
        if not self._listening:
            self._summary.setText("Cannot share: the connection port is already in use")
            return
        discoverable = (
            f'Discoverable on this LAN as "{self._name}"'
            if self._discoverable
            else "Not discoverable (discovery port in use)"
        )
        sharing = (
            f"Sharing this desktop with {client_count} viewer(s)"
            if client_count
            else "Sharing enabled — no viewers connected"
        )
        self._summary.setText(f"{discoverable}\n{sharing}")

    def revoke_client(self, client_id: str) -> None:
        """Revoke a client's pairing, whether or not sharing is running."""
        if self.share_server is not None:
            self.share_server.revoke_client(client_id)
            return
        self._paired.revoke(client_id)
        self.statusMessage.emit(f"Revoked access for client {client_id}")
        self.peerEvent.emit(
            {"key": client_id, "event": "revoked", "name": "", "address": "", "detail": client_id}
        )

    def request_client_log(self) -> None:
        if self.share_server is None:
            self.statusMessage.emit("Not sharing — no connected client to request a log from")
            return
        self.share_server.request_log()

    def _show_client_log(self, client_name: str, text: str) -> None:
        title = f'Log from client "{client_name}"' if client_name else "Log from client"
        PeerLogDialog(title, text, self).show()

    def _ask_approval(self, client_id: str, client_name: str) -> bool:
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "Connection request",
            f'Allow "{client_name}" to view this desktop?\n\nClient id: {client_id}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self.window(),
        )
        # The app usually isn't the foreground window when a request arrives
        # (it may even be hidden in the tray), and Windows won't give focus
        # to a background app's dialog — keep it on top so it can't sit
        # unnoticed behind other windows.
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.show()
        box.raise_()
        box.activateWindow()
        return box.exec() == QMessageBox.StandardButton.Yes
