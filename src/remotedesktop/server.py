"""Server GUI application: shares this computer's desktop with permitted
clients and prompts the user to approve first-time connections."""

import logging
import socket
import sqlite3
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import db, logs, tls, window_state
from remotedesktop.autostart import Autostart
from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import PairedClients, Settings, default_config_dir, default_db_path
from remotedesktop.discovery import (
    DEFAULT_CONNECT_PORT,
    DISCOVERY_PORT,
    DiscoveryResponder,
)
from remotedesktop.inventory import ConnectionInventory, InventoryTab
from remotedesktop.sharing import ShareServer

_log = logging.getLogger("remotedesktop.server")


class ServerWindow(QMainWindow):
    def __init__(
        self,
        *,
        discovery_port: int = DISCOVERY_PORT,
        connect_port: int = DEFAULT_CONNECT_PORT,
        paired: PairedClients | None = None,
        credentials=None,
        connection: sqlite3.Connection | None = None,
        autostart: Autostart | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Server")
        self._name = socket.gethostname()

        self._summary = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self._autostart = autostart if autostart is not None else Autostart()
        self.autostart_checkbox = QCheckBox("Start this server when I log in to Windows")
        self.autostart_checkbox.setChecked(self._autostart.is_enabled())
        self.autostart_checkbox.setEnabled(self._autostart.available)
        self.autostart_checkbox.toggled.connect(self._on_autostart_toggled)
        self.connection_log = QPlainTextEdit()
        self.connection_log.setReadOnly(True)
        self.connection_log.setMaximumBlockCount(1000)
        status_tab = QWidget()
        status_layout = QVBoxLayout(status_tab)
        status_layout.addWidget(self._summary)
        status_layout.addWidget(self.autostart_checkbox, alignment=Qt.AlignmentFlag.AlignHCenter)
        status_layout.addStretch(1)

        # Tests inject a connection to a temp database; the app uses the default.
        self._db = connection if connection is not None else db.connect(default_db_path())
        self.inventory = ConnectionInventory(self._db, "server_peers", self)
        tabs = QTabWidget()
        tabs.addTab(status_tab, "Status")
        tabs.addTab(
            InventoryTab(self.inventory, "Revoke access", self._revoke_client),
            "Clients on LAN",
        )
        tabs.addTab(self.connection_log, "Connection log")
        self.setCentralWidget(tabs)

        if credentials is None:
            config_dir = default_config_dir()
            credentials = tls.load_or_create_credentials(
                config_dir / "server_cert.pem", config_dir / "server_key.pem"
            )
        if paired is None:
            paired = PairedClients(self._db)
        self._clipboard = ClipboardSync(parent=self)
        self.share_server = ShareServer(
            self._ask_approval,
            credentials=credentials,
            paired=paired,
            clipboard=self._clipboard,
            parent=self,
        )
        self.share_server.status.connect(self.log)
        self.share_server.clientCountChanged.connect(self._update_summary)
        self.share_server.peerEvent.connect(self._record_peer)
        self._listening = self.share_server.listen(connect_port)

        self.responder: DiscoveryResponder | None = None
        self._discoverable = False
        if self._listening:
            responder = DiscoveryResponder(
                self._name, self.share_server.port, discovery_port=discovery_port
            )
            try:
                responder.start()
            except OSError as error:
                self.log(
                    f"Discovery unavailable (UDP port {discovery_port}): {error} — "
                    "another server may already be running"
                )
            else:
                self.responder = responder
                self._discoverable = True
                self.log(
                    f'Discoverable as "{self._name}" '
                    f"(UDP port {discovery_port}, TCP port {self.share_server.port})"
                )
        self._update_summary(0)
        self._settings = Settings(self._db)
        window_state.restore_geometry(self, self._settings, window_state.SERVER_GEOMETRY_KEY)

    def log(self, message: str) -> None:
        # Everything shown in the Connection log pane also goes to the debug
        # log file (when main() enabled it), so it survives the window.
        _log.info(message)
        self.connection_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    def _record_peer(self, event: dict) -> None:
        self.inventory.record(
            event["key"],
            event["event"],
            name=event.get("name", ""),
            address=event.get("address", ""),
            detail=event.get("detail", ""),
        )

    def _update_summary(self, client_count: int) -> None:
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
            else "Not sharing"
        )
        self._summary.setText(f"{discoverable}\n{sharing}")

    def _on_autostart_toggled(self, checked: bool) -> None:
        self._autostart.set_enabled(checked)
        self.log(
            "Server will start at login" if checked else "Server will no longer start at login"
        )

    def _revoke_client(self, client_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "Revoke access",
            f"Revoke access for client {client_id}?\n\n"
            "It will be disconnected now and must be approved again to reconnect.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.share_server.revoke_client(client_id)

    def _ask_approval(self, client_id: str, client_name: str) -> bool:
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "Connection request",
            f'Allow "{client_name}" to view this desktop?\n\nClient id: {client_id}',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        # The server usually isn't the foreground app when a request arrives,
        # and Windows won't give focus to a background app's dialog — keep it
        # on top so it can't sit unnoticed behind other windows.
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        box.show()
        box.raise_()
        box.activateWindow()
        return box.exec() == QMessageBox.StandardButton.Yes

    def closeEvent(self, event: QCloseEvent) -> None:
        window_state.save_geometry(self, self._settings, window_state.SERVER_GEOMETRY_KEY)
        if self.responder is not None:
            self.responder.stop()
        self.share_server.close()
        super().closeEvent(event)


def main() -> None:  # pragma: no cover - runs the Qt event loop
    log_path = logs.init_logging("server")
    app = QApplication(sys.argv)
    window = ServerWindow()
    window.log(f"Detailed log: {log_path}")
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":  # pragma: no cover
    main()
