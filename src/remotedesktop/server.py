"""Server GUI application: shares this computer's desktop with permitted
clients and prompts the user to approve first-time connections."""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow


class ServerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Server")
        self.setCentralWidget(
            QLabel("Not sharing", alignment=Qt.AlignmentFlag.AlignCenter)
        )


def main() -> None:
    app = QApplication(sys.argv)
    window = ServerWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
