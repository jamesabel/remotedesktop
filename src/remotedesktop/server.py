"""Server GUI application: shares this computer's desktop with permitted
clients and prompts the user to approve first-time connections."""

import socket
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import ApprovedClients
from remotedesktop.discovery import (
    DEFAULT_CONNECT_PORT,
    DISCOVERY_PORT,
    DiscoveryResponder,
)
from remotedesktop.sharing import ShareServer


class ServerWindow(QMainWindow):
    def __init__(
        self,
        *,
        discovery_port: int = DISCOVERY_PORT,
        connect_port: int = DEFAULT_CONNECT_PORT,
        approved: ApprovedClients | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Server")
        self._name = socket.gethostname()

        self._summary = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.connection_log = QPlainTextEdit()
        self.connection_log.setReadOnly(True)
        self.connection_log.setMaximumBlockCount(1000)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._summary)
        layout.addWidget(self.connection_log, stretch=1)
        self.setCentralWidget(central)

        self._clipboard = ClipboardSync(parent=self)
        self.share_server = ShareServer(
            self._ask_approval, approved=approved, clipboard=self._clipboard, parent=self
        )
        self.share_server.status.connect(self.log)
        self.share_server.clientCountChanged.connect(self._update_summary)
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

    def log(self, message: str) -> None:
        self.connection_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

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

    def _ask_approval(self, client_id: str, client_name: str) -> bool:
        answer = QMessageBox.question(
            self,
            "Connection request",
            f'Allow "{client_name}" to view this desktop?\n\nClient id: {client_id}',
        )
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.responder is not None:
            self.responder.stop()
        self.share_server.close()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = ServerWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
