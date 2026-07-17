"""Server GUI application: shares this computer's desktop with permitted
clients and prompts the user to approve first-time connections."""

import socket
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

from remotedesktop.discovery import (
    DEFAULT_CONNECT_PORT,
    DISCOVERY_PORT,
    DiscoveryResponder,
)


class ServerWindow(QMainWindow):
    def __init__(self, *, discovery_port: int = DISCOVERY_PORT) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Server")
        name = socket.gethostname()
        self.responder = DiscoveryResponder(
            name, DEFAULT_CONNECT_PORT, discovery_port=discovery_port
        )
        try:
            self.responder.start()
            status = f'Discoverable on this LAN as "{name}"\nNot sharing'
        except OSError:
            status = (
                "Discovery unavailable — another server may already be running\n"
                "Not sharing"
            )
        self.setCentralWidget(QLabel(status, alignment=Qt.AlignmentFlag.AlignCenter))

    def closeEvent(self, event: QCloseEvent) -> None:
        self.responder.stop()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = ServerWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
