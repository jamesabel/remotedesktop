"""Server GUI application: shares this computer's desktop with permitted
clients and prompts the user to approve first-time connections."""

import logging
import socket
import sqlite3
import sys
import time

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import db, icon, logs, tls, window_state
from remotedesktop.autostart import Autostart
from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import PairedClients, Settings, default_config_dir, default_db_path
from remotedesktop.discovery import (
    DEFAULT_CONNECT_PORT,
    DISCOVERY_PORT,
    DiscoveryResponder,
)
from remotedesktop.inventory import ConnectionInventory, InventoryTab
from remotedesktop.logs import PeerLogDialog, read_log_tail
from remotedesktop.modal_loop import ModalLoopPump
from remotedesktop.performance import PerformanceMonitor, PerformanceTab
from remotedesktop.preferences import PreferencesTab, load_performance_window_seconds
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
        self.setWindowIcon(icon.app_icon("server"))
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
        self.get_log_button = QPushButton("Get client log")
        self.get_log_button.setToolTip(
            "Ask the most recently connected client to send its debug log"
        )
        self.get_log_button.clicked.connect(self._request_client_log)
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.addWidget(self.get_log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        log_layout.addWidget(self.connection_log)
        # While this window sits in Windows' modal move/size loop (title-bar
        # drag — including one driven by an injected remote click), Qt stops
        # running; the pump keeps sockets and timers serviced so a remote
        # mouse-up can still arrive and end the drag instead of deadlocking.
        self._modal_pump = ModalLoopPump()
        self.restart_button = QPushButton("Restart server")
        self.restart_button.setToolTip(
            "Relaunch this app (e.g. after updating the software). It can be "
            "clicked from a remote desktop session, so an update doesn't "
            "require visiting this computer."
        )
        self.restart_button.clicked.connect(self._restart_server)
        status_tab = QWidget()
        status_layout = QVBoxLayout(status_tab)
        status_layout.addWidget(self._summary)
        status_layout.addWidget(self.autostart_checkbox, alignment=Qt.AlignmentFlag.AlignHCenter)
        status_layout.addWidget(self.restart_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        status_layout.addStretch(1)

        # Tests inject a connection to a temp database; the app uses the default.
        self._db = connection if connection is not None else db.connect(default_db_path())
        self._settings = Settings(self._db)
        self.performance = PerformanceMonitor(
            window_seconds=float(load_performance_window_seconds(self._settings)),
            parent=self,
        )
        self.inventory = ConnectionInventory(self._db, "server_peers", self)
        tabs = QTabWidget()
        tabs.addTab(status_tab, "Status")
        tabs.addTab(
            InventoryTab(self.inventory, "Revoke access", self._revoke_client),
            "Clients on LAN",
        )
        tabs.addTab(PerformanceTab(self.performance), "Performance")
        tabs.addTab(log_tab, "Connection log")
        tabs.addTab(PreferencesTab(self._settings, self.performance), "Preferences")
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
            performance=self.performance,
            log_provider=lambda: read_log_tail("server"),
            parent=self,
        )
        self.share_server.status.connect(self.log)
        self.share_server.clientCountChanged.connect(self._update_summary)
        self.share_server.peerEvent.connect(self._record_peer)
        self.share_server.logReceived.connect(self._show_client_log)
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

    def _restart_server(self) -> None:
        """Relaunch this app in a new process and exit.

        Meant to be clicked through a remote desktop session after updating
        the software, so the new version starts without anyone at this
        computer. The listening sockets are closed before spawning so the
        replacement can bind the same ports.
        """
        answer = QMessageBox.question(
            self,
            "Restart server",
            "Restart the server app?\n\n"
            "Viewers will be disconnected and can reconnect in a few seconds "
            "(approved clients reconnect without a new permission prompt).",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.log("Restarting: freeing ports and launching a new server process")
        _log.info("Restart requested — relaunching %s -m remotedesktop.server", sys.executable)
        if self.responder is not None:
            self.responder.stop()
            self.responder = None
        self.share_server.close()
        if not QProcess.startDetached(sys.executable, ["-m", "remotedesktop.server"]):
            # Extremely unlikely (sys.executable exists); the app stays open —
            # sharing is stopped, but the machine isn't left with nothing.
            self.log("Restart failed: could not launch a new process — restart manually")
            _log.error("QProcess.startDetached failed for %s", sys.executable)
            return
        self.close()
        QApplication.quit()

    def _request_client_log(self) -> None:
        self.share_server.request_log()

    def _show_client_log(self, client_name: str, text: str) -> None:
        title = f'Log from client "{client_name}"' if client_name else "Log from client"
        PeerLogDialog(title, text, self).show()

    def nativeEvent(self, event_type, message):
        self._modal_pump.handle_native_event(event_type, message)
        return super().nativeEvent(event_type, message)

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
    icon.set_windows_app_id("remotedesktop.server")
    app = QApplication(sys.argv)
    app.setWindowIcon(icon.app_icon("server"))
    window = ServerWindow()
    window.log(f"Detailed log: {log_path}")
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":  # pragma: no cover
    main()
