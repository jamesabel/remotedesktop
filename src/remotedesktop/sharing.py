"""Screen sharing over TCP.

ShareServer (server side) accepts clients, enforces first-connection
approval, and streams JPEG frames of the primary screen to all connected
clients from a single capture timer. ShareClient (client side) connects,
identifies itself, and emits decoded frames.

Both classes emit human-readable `status` messages for every phase of the
connection so the GUIs can show a debug log.
"""

import socket as socket_module
from collections.abc import Callable

from PySide6.QtCore import QBuffer, QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from remotedesktop.config import ApprovedClients, load_client_identity
from remotedesktop.discovery import DEFAULT_CONNECT_PORT
from remotedesktop.protocol import PROTOCOL_VERSION, MessageStream

DEFAULT_FPS = 10
JPEG_QUALITY = 70
# Skip sending to a client whose socket buffer is this far behind.
_MAX_SEND_BACKLOG = 8 * 1024 * 1024


def _peer(sock: QTcpSocket) -> str:
    return f"{sock.peerAddress().toString()}:{sock.peerPort()}"


class ShareServer(QObject):
    """Listens for clients, enforces first-connection approval, streams the screen."""

    clientCountChanged = Signal(int)
    status = Signal(str)

    def __init__(
        self,
        approve_client: Callable[[str, str], bool],
        *,
        approved: ApprovedClients | None = None,
        fps: int = DEFAULT_FPS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._approve_client = approve_client
        self._approved = approved if approved is not None else ApprovedClients()
        self._fps = fps
        self._streams: list[MessageStream] = []
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._timer = QTimer(self)
        self._timer.setInterval(round(1000 / fps))
        self._timer.timeout.connect(self._broadcast_frame)

    def listen(self, port: int = DEFAULT_CONNECT_PORT) -> bool:
        if not self._server.listen(QHostAddress.SpecialAddress.Any, port):
            self.status.emit(
                f"Cannot listen on TCP port {port}: {self._server.errorString()}"
            )
            return False
        self.status.emit(f"Listening for connections on TCP port {self.port}")
        return True

    @property
    def port(self) -> int:
        return self._server.serverPort()

    def close(self) -> None:
        self._timer.stop()
        for stream in self._streams:
            stream.socket.abort()
        if self._streams:
            self._streams.clear()
            self.clientCountChanged.emit(0)
        self._server.close()
        self.status.emit("Server closed")

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self.status.emit(f"Incoming connection from {_peer(sock)} — waiting for hello")
            stream = MessageStream(sock, self)
            sock.disconnected.connect(lambda s=stream: self._drop(s))
            sock.errorOccurred.connect(
                lambda _error, s=sock: self.status.emit(
                    f"Socket error ({_peer(s)}): {s.errorString()}"
                )
            )
            stream.jsonReceived.connect(lambda m, s=stream: self._on_message(s, m))

    def _on_message(self, stream: MessageStream, message: dict) -> None:
        if message.get("type") != "hello" or stream in self._streams:
            return
        peer = _peer(stream.socket)
        client_id = message.get("client_id")
        client_name = str(message.get("name", "unknown"))
        self.status.emit(f'Hello from "{client_name}" ({client_id}) at {peer}')
        if message.get("version") != PROTOCOL_VERSION:
            self.status.emit(
                f"Denying {peer}: incompatible protocol version {message.get('version')}"
            )
            stream.send_json({"type": "denied", "reason": "incompatible protocol version"})
            stream.socket.disconnectFromHost()
            return
        if not isinstance(client_id, str) or not client_id:
            self.status.emit(f"Denying {peer}: missing client id")
            stream.socket.abort()
            return
        if client_id in self._approved:
            self.status.emit(f'"{client_name}" is already approved')
        else:
            self.status.emit(f'"{client_name}" is not yet approved — asking for permission')
            if not self._approve_client(client_id, client_name):
                self.status.emit(f'Permission for "{client_name}" refused — denying')
                stream.send_json({"type": "denied", "reason": "connection refused by user"})
                stream.socket.disconnectFromHost()
                return
            self._approved.add(client_id)
            self.status.emit(f'Permission granted — "{client_name}" added to approved clients')
        stream.send_json({"type": "welcome", "name": socket_module.gethostname()})
        self._streams.append(stream)
        self.clientCountChanged.emit(len(self._streams))
        self.status.emit(
            f'Streaming screen to "{client_name}" at {self._fps} fps '
            f"({len(self._streams)} viewer(s) total)"
        )
        if not self._timer.isActive():
            self._timer.start()

    def _drop(self, stream: MessageStream) -> None:
        if stream not in self._streams:
            self.status.emit("Connection closed before completing hello")
            return
        self._streams.remove(stream)
        self.clientCountChanged.emit(len(self._streams))
        self.status.emit(f"Client disconnected — {len(self._streams)} viewer(s) remaining")
        if not self._streams:
            self._timer.stop()
            self.status.emit("No viewers left — screen capture stopped")

    def _broadcast_frame(self) -> None:
        if not self._streams:
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        image = screen.grabWindow(0).toImage()
        if image.isNull():
            self.status.emit("Screen capture failed (null image)")
            return
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(buffer, "JPEG", JPEG_QUALITY)
        jpeg = bytes(buffer.data())
        for stream in self._streams:
            if stream.socket.bytesToWrite() > _MAX_SEND_BACKLOG:
                continue  # client is not keeping up; drop this frame for it
            stream.send_frame(jpeg)


class ShareClient(QObject):
    """Connects to a ShareServer, identifies itself, and emits decoded frames."""

    connected = Signal(str)  # server name
    denied = Signal(str)  # reason
    disconnected = Signal()
    frameReceived = Signal(QImage)
    status = Signal(str)

    def __init__(
        self, identity: tuple[str, str] | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._client_id, self._name = identity or load_client_identity()
        self._got_first_frame = False
        self._socket = QTcpSocket(self)
        self._stream = MessageStream(self._socket, self)
        self._socket.connected.connect(self._send_hello)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.errorOccurred.connect(
            lambda _error: self.status.emit(f"Socket error: {self._socket.errorString()}")
        )
        self._stream.jsonReceived.connect(self._on_message)
        self._stream.frameReceived.connect(self._on_frame)

    def connect_to(self, host: str, port: int) -> None:
        self._got_first_frame = False
        self.status.emit(f"Connecting to {host}:{port} …")
        self._socket.connectToHost(host, port)

    def close(self) -> None:
        self._socket.abort()

    def _send_hello(self) -> None:
        self.status.emit(
            f'TCP connected — sending hello as "{self._name}" (client id {self._client_id})'
        )
        self._stream.send_json(
            {
                "type": "hello",
                "version": PROTOCOL_VERSION,
                "client_id": self._client_id,
                "name": self._name,
            }
        )

    def _on_disconnected(self) -> None:
        self.status.emit("Disconnected from server")
        self.disconnected.emit()

    def _on_message(self, message: dict) -> None:
        match message.get("type"):
            case "welcome":
                name = str(message.get("name", ""))
                self.status.emit(
                    f'Server "{name}" accepted the connection — waiting for first frame'
                )
                self.connected.emit(name)
            case "denied":
                reason = str(message.get("reason", "denied"))
                self.status.emit(f"Server denied the connection: {reason}")
                self.denied.emit(reason)

    def _on_frame(self, jpeg: bytes) -> None:
        image = QImage.fromData(jpeg, "JPEG")
        if image.isNull():
            self.status.emit(f"Received undecodable frame ({len(jpeg)} bytes)")
            return
        if not self._got_first_frame:
            self._got_first_frame = True
            self.status.emit(
                f"First frame received: {image.width()}x{image.height()} "
                f"({len(jpeg) // 1024} KB as JPEG)"
            )
        self.frameReceived.emit(image)
