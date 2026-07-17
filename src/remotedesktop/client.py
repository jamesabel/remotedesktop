"""Client GUI application: discovers servers on the LAN, connects to one,
and shows its desktop in a viewer widget."""

import sys

from PySide6.QtWidgets import QApplication, QMainWindow

from remotedesktop.viewer import ViewerWidget


class ClientWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Remote Desktop Client")
        self.viewer = ViewerWidget(self)
        self.setCentralWidget(self.viewer)


def main() -> None:
    app = QApplication(sys.argv)
    window = ClientWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
