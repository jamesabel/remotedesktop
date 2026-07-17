"""Length-prefixed message framing over a QTcpSocket.

Wire format: a 4-byte big-endian payload length, one kind byte (JSON control
message or JPEG screen frame), then the payload. Malformed or oversized
input aborts the connection.
"""

import json
import struct

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QTcpSocket

PROTOCOL_VERSION = 1

_HEADER = struct.Struct(">IB")
_KIND_JSON = 0
_KIND_FRAME = 1
_MAX_PAYLOAD = 64 * 1024 * 1024


class MessageStream(QObject):
    """Sends and receives framed messages on an existing QTcpSocket."""

    jsonReceived = Signal(dict)
    frameReceived = Signal(bytes)

    def __init__(self, socket: QTcpSocket, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.socket = socket
        socket.readyRead.connect(self._on_ready_read)

    def send_json(self, message: dict) -> None:
        self._send(_KIND_JSON, json.dumps(message).encode())

    def send_frame(self, jpeg: bytes) -> None:
        self._send(_KIND_FRAME, jpeg)

    def _send(self, kind: int, payload: bytes) -> None:
        self.socket.write(_HEADER.pack(len(payload), kind) + payload)

    def _on_ready_read(self) -> None:
        while True:
            if self.socket.bytesAvailable() < _HEADER.size:
                return
            length, kind = _HEADER.unpack(self.socket.peek(_HEADER.size).data())
            if length > _MAX_PAYLOAD:
                self.socket.abort()
                return
            if self.socket.bytesAvailable() < _HEADER.size + length:
                return
            self.socket.read(_HEADER.size)
            payload = self.socket.read(length).data()
            if kind == _KIND_JSON:
                try:
                    message = json.loads(payload.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.socket.abort()
                    return
                if isinstance(message, dict):
                    self.jsonReceived.emit(message)
            elif kind == _KIND_FRAME:
                self.frameReceived.emit(payload)
            # Unknown kinds are skipped so the protocol can grow.
