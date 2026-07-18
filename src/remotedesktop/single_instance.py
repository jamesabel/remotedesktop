"""Allow only one instance of the app per user session.

`SingleInstance.acquire()` claims a named QLocalServer (a named pipe on
Windows, scoped to the user session). If another instance already holds it,
acquire() instead sends that instance an "activate" message — so launching
the app a second time (shortcut, autostart, `--minimized`) brings the
existing window to the front, even when it is hidden in the tray — and
returns False, telling the caller to exit.

A stale socket left by a crashed instance is handled: if nothing answers on
the socket, the name is removed and claimed. Tests pass a unique `name` so
parallel test runs never collide with each other or a real app.
"""

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_log = logging.getLogger("remotedesktop.single_instance")

_DEFAULT_NAME = "remotedesktop-single-instance"
_ACTIVATE = b"activate"
_CONNECT_TIMEOUT_MS = 500


class SingleInstance(QObject):
    """Named-local-socket guard; see module docstring."""

    activateRequested = Signal()  # another instance was launched and yielded to us

    def __init__(self, name: str = _DEFAULT_NAME, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """Claim the instance lock, or wake the holder and return False."""
        probe = QLocalSocket()
        probe.connectToServer(self._name)
        if probe.waitForConnected(_CONNECT_TIMEOUT_MS):
            probe.write(_ACTIVATE)
            probe.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
            probe.disconnectFromServer()
            _log.info("Another instance is already running — asked it to show itself")
            return False
        # Nothing listening: remove a stale socket from a crashed instance,
        # then claim the name.
        QLocalServer.removeServer(self._name)
        server = QLocalServer(self)
        server.newConnection.connect(self._on_connection)
        if not server.listen(self._name):
            # Extremely unlikely after removeServer; run unguarded rather
            # than refuse to start.
            _log.warning("Single-instance lock unavailable: %s", server.errorString())
            return True
        self._server = server
        return True

    def _on_connection(self) -> None:
        socket = self._server.nextPendingConnection() if self._server else None
        if socket is None:
            return
        # Any connection means "another launch happened"; the payload is
        # informational only.
        socket.readyRead.connect(socket.readAll)
        socket.disconnected.connect(socket.deleteLater)
        self.activateRequested.emit()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
