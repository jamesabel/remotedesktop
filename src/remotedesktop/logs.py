"""Persistent debug logging for the client and server apps.

The GUI "Connection log" panes only live as long as their window, which makes
an intermittent connection problem impossible to diagnose after the fact.
`init_logging` attaches a rotating file handler to the "remotedesktop" logger
so everything the panes show — plus lower-level socket and protocol detail
that is logged directly — survives on disk with millisecond timestamps.

Only the app entry points call `init_logging`; library code and tests just
log to `logging.getLogger("remotedesktop...")`, which is a no-op without a
handler.
"""

import logging
import logging.handlers
from pathlib import Path

import platformdirs
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout, QWidget

# Cap on what a peer gets when it asks for our log: the most recent entries
# are what matter, and the reply must stay a comfortable single message.
TAIL_BYTES = 1_000_000


def default_log_dir() -> Path:
    return Path(platformdirs.user_log_dir("remotedesktop", appauthor=False))


def log_path(app_name: str, *, directory: Path | None = None) -> Path:
    log_dir = directory if directory is not None else default_log_dir()
    return log_dir / f"{app_name}.log"


def read_log_tail(
    app_name: str, *, directory: Path | None = None, max_bytes: int = TAIL_BYTES
) -> str:
    """The last `max_bytes` of the app's debug log, for sending to a peer.

    Log exchange is a diagnostic aid and must never break a connection, so a
    missing or unreadable log yields a placeholder line instead of raising.
    """
    path = log_path(app_name, directory=directory)
    try:
        with path.open("rb") as file:
            size = file.seek(0, 2)
            file.seek(max(0, size - max_bytes))
            data = file.read(max_bytes)
    except OSError:
        return f"(no log available: {path} cannot be read)"
    return data.decode("utf-8", errors="replace")


class PeerLogDialog(QDialog):
    """Non-modal, read-only viewer for a log received from the peer."""

    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(900, 600)
        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        view.setPlainText(text)
        # The most recent entries matter most: start scrolled to the end.
        view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
        layout = QVBoxLayout(self)
        layout.addWidget(view)


def init_logging(app_name: str, *, directory: Path | None = None) -> Path:
    """Write "remotedesktop" logger output to <log dir>/<app_name>.log.

    Returns the log file path. `directory` exists for tests; the apps use
    the platformdirs log dir.
    """
    log_dir = directory if directory is not None else default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_path(app_name, directory=directory)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger = logging.getLogger("remotedesktop")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return path
