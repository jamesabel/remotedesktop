"""Generate the README demo GIFs (docs/media/*.gif) from synthetic data.

Runs the real client and server GUI apps in one process, connected over
loopback TLS with temp-file databases, and drives a short scripted session:
discovery, approval, live streaming of a *drawn* fake desktop (the
ShareServer._capture test seam — no real screen content is ever captured),
tab switches. Both windows are rendered with WA_DontShowOnScreen, grabbed on
a timer, and assembled into GIFs with Pillow.

Machine identities are synthetic too ("DEN-PC" serving, "LAPTOP" viewing,
user "alex"), so nothing about the generating machine leaks into the README.

Usage:  uv run python tools/make_demo_gifs.py [output_dir]
        (default output_dir: docs/media)
"""

import io
import math
import socket
import sys
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPolygonF
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget

from remotedesktop import db, sharing, tls
from remotedesktop.autostart import Autostart
from remotedesktop.client import ClientWindow
from remotedesktop.config import PairedClients, Settings
from remotedesktop.discovery import ServerInfo
from remotedesktop.server import ServerWindow
from remotedesktop import client as client_module

SCREEN_W, SCREEN_H = 1280, 800
GIF_WIDTH = 900
CAPTURE_INTERVAL = 0.18  # seconds per frame, also the GIF frame duration
TOTAL_SECONDS = 26.5
_GIF_COLORS = 96  # adaptive-palette size; the UI + fake desktop quantize well
_CODE_LINES = [
    "def changed_bands(previous, current):",
    '    """Bands of `current` that differ from `previous`."""',
    "    stride = current.bytesPerLine()",
    "    bands, y = [], 0",
    "    while y < current.height():",
    "        h = min(BAND_HEIGHT, height - y)",
    "        if prev[y*stride:(y+h)*stride] != cur[y*stride:(y+h)*stride]:",
    "            bands.append((y, h))",
    "        y += h",
    "    return bands",
    "",
    "class ShareServer(QObject):",
    "    clientCountChanged = Signal(int)",
    "    def listen(self, port):",
    "        return self._server.listen(Any, port)",
]


class SyntheticDesktop:
    """Draws an animated fake desktop: wallpaper, an editor window with
    scrolling code, a terminal appending lines, a clock, a gliding cursor.

    The gradient colors distinguish the two demo servers' desktops at a
    glance when the client shows one tab per server."""

    def __init__(
        self,
        gradient: tuple[tuple[int, int, int], tuple[int, int, int]] = (
            (16, 42, 84),
            (52, 18, 90),
        ),
    ) -> None:
        self._start = time.monotonic()
        self._background = self._draw_background(gradient)

    @staticmethod
    def _draw_background(colors: tuple[tuple[int, int, int], tuple[int, int, int]]) -> QImage:
        image = QImage(SCREEN_W, SCREEN_H, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        gradient = QLinearGradient(0, 0, SCREEN_W, SCREEN_H)
        gradient.setColorAt(0.0, QColor(*colors[0]))
        gradient.setColorAt(1.0, QColor(*colors[1]))
        painter.fillRect(0, 0, SCREEN_W, SCREEN_H, gradient)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for i, radius in enumerate((260, 180, 120)):
            painter.setBrush(QColor(255, 255, 255, 10 + i * 4))
            painter.drawEllipse(QPointF(1020 + i * 40, 190 - i * 20), radius, radius)
        painter.end()
        return image

    def grab(self) -> QImage:
        t = time.monotonic() - self._start
        image = self._background.copy()
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._editor(painter, t)
        self._terminal(painter, t)
        self._taskbar(painter, t)
        self._cursor(painter, t)
        painter.end()
        return image

    def _window_frame(self, painter: QPainter, x: int, y: int, w: int, h: int, title: str) -> None:
        painter.setPen(QColor(70, 70, 70))
        painter.setBrush(QColor(32, 33, 36))
        painter.drawRoundedRect(x, y, w, h, 6, 6)
        painter.fillRect(x + 1, y + 1, w - 2, 30, QColor(48, 49, 52))
        painter.setPen(QColor(220, 220, 220))
        painter.setFont(QFont("Segoe UI", 10))
        painter.drawText(x + 12, y + 21, title)
        for i, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(x + w - 20 - i * 22, y + 15), 6, 6)

    def _editor(self, painter: QPainter, t: float) -> None:
        x, y, w, h = 60, 60, 720, 520
        self._window_frame(painter, x, y, w, h, "frames.py — editor")
        painter.setClipRect(x + 1, y + 32, w - 2, h - 40)
        painter.setFont(QFont("Consolas", 11))
        offset = int(t * 22) % (len(_CODE_LINES) * 24)
        for i in range(24):
            line = _CODE_LINES[(i + offset // 24) % len(_CODE_LINES)]
            line_y = y + 56 + i * 24 - offset % 24
            painter.setPen(QColor(90, 96, 105))
            painter.drawText(x + 16, line_y, f"{(i + offset // 24) % 99 + 1:>3}")
            keyword = line.lstrip().startswith(("def ", "class ", "return", "while", "if "))
            painter.setPen(QColor("#c586c0") if keyword else QColor("#9cdcfe"))
            painter.drawText(x + 56, line_y, line)
        painter.setClipping(False)

    def _terminal(self, painter: QPainter, t: float) -> None:
        x, y, w, h = 620, 320, 580, 360
        self._window_frame(painter, x, y, w, h, "PowerShell")
        painter.setClipRect(x + 1, y + 32, w - 2, h - 40)
        painter.setFont(QFont("Consolas", 10))
        lines = int(t * 2) + 1
        for i in range(min(lines, 12)):
            step = lines - min(lines, 12) + i
            painter.setPen(QColor("#4ec9b0"))
            painter.drawText(x + 14, y + 54 + i * 26, f"PS> uv run pytest -q  # run {step}")
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(x + 300, y + 54 + i * 26, f"{170 + step} passed")
        painter.setClipping(False)

    def _taskbar(self, painter: QPainter, t: float) -> None:
        painter.fillRect(0, SCREEN_H - 44, SCREEN_W, 44, QColor(24, 24, 28))
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(6):
            painter.setBrush(QColor(70 + i * 12, 120, 200, 180))
            painter.drawRoundedRect(16 + i * 52, SCREEN_H - 36, 40, 28, 4, 4)
        painter.setPen(QColor(230, 230, 230))
        painter.setFont(QFont("Segoe UI", 10))
        clock = f"{(14 + int(t) // 3600) % 24:02d}:{(31 + int(t) // 60) % 60:02d}:{int(t) % 60:02d}"
        painter.drawText(SCREEN_W - 90, SCREEN_H - 16, clock)

    def _cursor(self, painter: QPainter, t: float) -> None:
        cx = 640 + 380 * math.sin(t * 0.9)
        cy = 400 + 240 * math.sin(t * 0.6 + 1.3)
        arrow = QPolygonF(
            [
                QPointF(cx, cy),
                QPointF(cx, cy + 21),
                QPointF(cx + 6, cy + 16),
                QPointF(cx + 13, cy + 15),
            ]
        )
        painter.setPen(QColor(0, 0, 0))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPolygon(arrow)


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("", 0))
        return probe.getsockname()[1]


def tab_index(tabs: QTabWidget, label: str) -> int:
    for index in range(tabs.count()):
        if tabs.tabText(index) == label:
            return index
    raise LookupError(label)


def to_gif_frame(window) -> Image.Image:
    image = window.grab().toImage().scaledToWidth(
        GIF_WIDTH, Qt.TransformationMode.SmoothTransformation
    )
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    png = bytes(buffer.data())  # ty: ignore[invalid-argument-type]
    return Image.open(io.BytesIO(png)).convert("RGB")


def save_gif(frames: list[Image.Image], path: Path) -> None:
    quantized = [
        f.convert("P", palette=Image.Palette.ADAPTIVE, colors=_GIF_COLORS) for f in frames
    ]
    quantized[0].save(
        path,
        save_all=True,
        append_images=quantized[1:],
        duration=int(CAPTURE_INTERVAL * 1000),
        loop=0,
        optimize=True,
    )
    print(f"{path}: {len(frames)} frames, {path.stat().st_size // 1024} KB")


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/media")
    out_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)

    # Everything below is synthetic: names, users, databases, screen content.
    socket.gethostname = lambda: "DEN-PC"  # ty: ignore[invalid-assignment]
    sharing._client_details = lambda: {  # ty: ignore[invalid-assignment]
        "user": "alex",
        "host": "LAPTOP",
        "os": "Windows 11 (10.0.26200)",
    }
    QMessageBox.show = lambda self: None  # approval prompt answers itself
    QMessageBox.exec = lambda self: QMessageBox.StandardButton.Yes

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        credentials = tls.load_or_create_credentials(
            tmp_path / "cert.pem", tmp_path / "key.pem"
        )
        server_db = db.connect(tmp_path / "server.db")
        server_window = ServerWindow(
            discovery_port=free_udp_port(),  # ephemeral: invisible to the real LAN
            connect_port=0,
            paired=PairedClients(server_db),
            credentials=credentials,
            connection=server_db,
            autostart=Autostart(key_path=r"Software\remotedesktop-tests\Demo", value_name="demo"),
        )
        desktop = SyntheticDesktop()
        server_window.share_server._capture = desktop.grab  # the test seam  # ty: ignore[invalid-assignment]

        # A second, window-less server ("LAB-PC", teal desktop) so the client
        # demo shows one tab per connected server.
        lab_db = db.connect(tmp_path / "lab.db")
        lab_server = sharing.ShareServer(
            approve_client=lambda _cid, _name: True,
            credentials=credentials,
            paired=PairedClients(lab_db),
        )
        lab_desktop = SyntheticDesktop(gradient=((10, 62, 52), (14, 34, 78)))
        lab_server._capture = lab_desktop.grab  # ty: ignore[invalid-assignment]
        assert lab_server.listen(0)

        client_db = db.connect(tmp_path / "client.db")
        client_settings = Settings(client_db)
        client_settings.set("client_id", str(uuid.uuid4()))
        client_settings.set("client_name", "LAPTOP")
        client_window = ClientWindow(connection=client_db, auto_scan=False)

        for window, size in ((server_window, (1180, 620)), (client_window, (1180, 760))):
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            window.resize(*size)
            window.show()

        info = ServerInfo(name="DEN-PC", host="127.0.0.1", port=server_window.share_server.port)
        lab_info = ServerInfo(name="LAB-PC", host="127.0.0.1", port=lab_server.port)
        client_module.discover_servers = lambda: [info, lab_info]  # ty: ignore[invalid-assignment]
        client_tabs = client_window.centralWidget()
        server_tabs = server_window.centralWidget()
        assert isinstance(client_tabs, QTabWidget) and isinstance(server_tabs, QTabWidget)

        # The live remote-screen section is the product's whole point, so it
        # gets the lion's share of the runtime: DEN-PC streams alone first,
        # then LAB-PC joins (one tab per server) and the demo flips between
        # the two tabs before the brief Performance detour and closing shot.
        # gethostname is swapped just before the second connection so LAB-PC's
        # welcome reports its own name (it is read at admission time).
        script = [
            (0.8, lambda: client_window.discovery_panel.refresh()),
            (2.0, lambda: client_window.discovery_panel.serverActivated.emit(info)),
            (7.0, lambda: setattr(socket, "gethostname", lambda: "LAB-PC")),
            (7.2, lambda: client_window.discovery_panel.serverActivated.emit(lab_info)),
            (13.0, lambda: client_tabs.setCurrentIndex(tab_index(client_tabs, "DEN-PC"))),
            (17.0, lambda: client_tabs.setCurrentIndex(tab_index(client_tabs, "LAB-PC"))),
            (21.2, lambda: client_tabs.setCurrentIndex(tab_index(client_tabs, "Performance"))),
            (21.6, lambda: server_tabs.setCurrentIndex(tab_index(server_tabs, "Performance"))),
            (23.6, lambda: client_tabs.setCurrentIndex(tab_index(client_tabs, "DEN-PC"))),
            (24.0, lambda: server_tabs.setCurrentIndex(tab_index(server_tabs, "Status"))),
        ]
        client_frames: list[Image.Image] = []
        server_frames: list[Image.Image] = []
        start = time.monotonic()
        next_capture = 0.0
        while (elapsed := time.monotonic() - start) < TOTAL_SECONDS:
            while script and elapsed >= script[0][0]:
                script.pop(0)[1]()
            if elapsed >= next_capture:
                client_frames.append(to_gif_frame(client_window))
                server_frames.append(to_gif_frame(server_window))
                next_capture += CAPTURE_INTERVAL
            app.processEvents()
            time.sleep(0.005)

        save_gif(client_frames, out_dir / "client-demo.gif")
        save_gif(server_frames, out_dir / "server-demo.gif")
        client_window.close()
        server_window.close()
        lab_server.close()
        app.processEvents()
        # The temp dir can't be removed while the SQLite files are open.
        client_db.close()
        server_db.close()
        lab_db.close()


if __name__ == "__main__":
    main()
