"""Screen sharing over TLS.

The transport is TLS (the server has a persisted self-signed certificate; the
client trusts it on first use and pins its fingerprint). On top of that, a
simple token handshake authenticates the client: the first time a client
connects, the server-side user approves it and the server issues a shared
token, which the client stores and presents on later connections to reconnect
without another prompt. A lost or mismatched token just falls back to asking
the user again — nothing hard-fails — which keeps reconnection robust on a
trusted LAN.

Both classes emit human-readable `status` messages for every phase so the
GUIs can show a debug log.
"""

import hmac
import logging
import socket as socket_module
from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtNetwork import (
    QAbstractSocket,
    QHostAddress,
    QSslCertificate,
    QSslKey,
    QSslServer,
    QSslSocket,
    QTcpSocket,
)

from remotedesktop.config import (
    KnownServers,
    PairedClients,
    default_db_path,
    load_client_identity,
)
from remotedesktop.discovery import DEFAULT_CONNECT_PORT
from remotedesktop import frames
from remotedesktop.input_injection import InputInjector
from remotedesktop.performance import PerformanceMonitor
from remotedesktop.protocol import MAX_PAYLOAD, PROTOCOL_VERSION, MessageStream
from remotedesktop import db, tls

_log = logging.getLogger("remotedesktop.sharing")

# 30 fps keeps key-to-glyph latency low (~17 ms average sampling delay).
# Affordable since inter-frame compression: an unchanged screen costs only
# a capture and a memory compare per tick — nothing is encoded or sent.
DEFAULT_FPS = 30
# Only for full frames to legacy (0.5.0) clients, which force-decode frames
# as JPEG. Delta-capable clients get lossless PNG keyframes and delta bands.
JPEG_QUALITY = 70
# Skip sending to a client whose socket buffer is this far behind.
_MAX_SEND_BACKLOG = 8 * 1024 * 1024
# Until a client passes the approval handshake it may only send small
# messages (a hello is well under 1 KB); the cap is lifted on admission.
_PREAUTH_MAX_PAYLOAD = 64 * 1024


def _peer(sock: QTcpSocket) -> str:
    return f"{sock.peerAddress().toString()}:{sock.peerPort()}"


def _coord(value) -> float:
    """Coerce a coordinate from an input message; malformed values raise
    TypeError/ValueError, which the caller treats as a bad message."""
    if value is None:
        raise TypeError("missing coordinate")
    return float(value)


class ShareServer(QObject):
    """Listens over TLS, authenticates clients by token, streams the screen."""

    clientCountChanged = Signal(int)
    status = Signal(str)
    peerEvent = Signal(dict)  # {key, event, name, address, detail} for the inventory

    def __init__(
        self,
        approve_client: Callable[[str, str], bool],
        *,
        credentials: tuple[QSslCertificate, QSslKey] | None = None,
        paired: PairedClients | None = None,
        fps: int = DEFAULT_FPS,
        injector: InputInjector | None = None,
        clipboard=None,
        performance: PerformanceMonitor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._performance = performance
        self._approve_client = approve_client
        self._paired = paired if paired is not None else PairedClients(db.connect(default_db_path()))
        self._fps = fps
        self._injector = injector if injector is not None else InputInjector()
        self._clipboard = clipboard
        if clipboard is not None:
            clipboard.changed.connect(self._broadcast_clipboard)
        self._streams: list[MessageStream] = []
        self._all_streams: set[MessageStream] = set()
        self._controllers: set[MessageStream] = set()
        self._stream_key: dict[MessageStream, tuple[str, str, str]] = {}
        self._revoked: set[str] = set()
        # Streams whose approval prompt is currently open (repeat hellos are
        # ignored meanwhile) and streams whose terminal inventory state
        # (denied/refused) is already recorded, so _drop must not overwrite it.
        self._prompting: set[MessageStream] = set()
        self._final: set[MessageStream] = set()
        # What each controlling stream currently holds down: (buttons, vks).
        self._pressed: dict[MessageStream, tuple[set[str], set[int]]] = {}
        # Streams currently too far behind to receive frames (see
        # _broadcast_frame); entering/leaving this set emits a status message.
        self._backlogged: set[MessageStream] = set()
        # Inter-frame compression state: which admitted streams understand
        # delta frames (hello carried "delta": true), which need a full
        # keyframe next broadcast (just admitted, just caught up after a
        # backlog, or asked for one), and the previous capture to diff.
        self._delta_capable: set[MessageStream] = set()
        self._needs_keyframe: set[MessageStream] = set()
        self._previous_frame: QImage | None = None

        cert, key = credentials if credentials is not None else tls.ephemeral_credentials()
        self._server = QSslServer(self)
        self._server.setSslConfiguration(tls.server_configuration(cert, key))
        self._server.pendingConnectionAvailable.connect(self._on_new_connection)
        self._server.errorOccurred.connect(
            lambda sock, _err: self.status.emit(
                f"TLS handshake error ({_peer(sock)}): {sock.errorString()}"
            )
        )
        self._timer = QTimer(self)
        self._timer.setInterval(round(1000 / fps))
        self._timer.timeout.connect(self._broadcast_frame)

    def listen(self, port: int = DEFAULT_CONNECT_PORT) -> bool:
        if not self._server.listen(QHostAddress.SpecialAddress.Any, port):
            self.status.emit(
                f"Cannot listen on TCP port {port}: {self._server.errorString()}"
            )
            return False
        self.status.emit(f"Listening for TLS connections on TCP port {self.port}")
        return True

    @property
    def port(self) -> int:
        return self._server.serverPort()

    def close(self) -> None:
        self._timer.stop()
        for stream in self._all_streams:
            # Silence disconnected/error so _drop isn't re-entered during teardown
            # (covers streams that never completed the hello, too).
            stream.socket.blockSignals(True)
            stream.socket.abort()
            self._release_input(stream)
            stream.socket.deleteLater()
            stream.deleteLater()
        had_clients = bool(self._streams)
        self._streams.clear()
        self._all_streams.clear()
        self._controllers.clear()
        self._stream_key.clear()
        self._prompting.clear()
        self._final.clear()
        self._pressed.clear()
        self._backlogged.clear()
        self._delta_capable.clear()
        self._needs_keyframe.clear()
        self._previous_frame = None
        if had_clients:
            self.clientCountChanged.emit(0)
        if self._performance is not None:
            self._performance.reset()
        self._server.close()
        self.status.emit("Server closed")

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            # QSslServer hands out QSslSockets; the stubs say QTcpSocket.
            sock = cast(QSslSocket, self._server.nextPendingConnection())
            # TCP_NODELAY: input events and delta frames are small messages;
            # Nagle + delayed ACK would add tens to hundreds of ms of latency.
            sock.setSocketOption(QAbstractSocket.SocketOption.LowDelayOption, 1)
            self.status.emit(
                f"Incoming TLS connection from {_peer(sock)} "
                f"(encrypted={sock.isEncrypted()}) — waiting for hello"
            )
            stream = MessageStream(sock, self, max_payload=_PREAUTH_MAX_PAYLOAD)
            self._all_streams.add(stream)
            sock.stateChanged.connect(
                lambda state, s=sock: _log.debug("Socket %s state: %s", _peer(s), state.name)
            )
            sock.disconnected.connect(lambda s=stream: self._drop(s))
            sock.errorOccurred.connect(
                lambda _error, s=sock: self.status.emit(
                    f"Socket error ({_peer(s)}): {s.errorString()}"
                )
            )
            stream.jsonReceived.connect(lambda m, s=stream: self._on_message(s, m))

    def _on_message(self, stream: MessageStream, message: dict) -> None:
        if message.get("type") in ("ping", "pong"):
            # Same admission rule as input/clipboard: pre-auth peers get nothing.
            if stream in self._streams and self._performance is not None:
                self._performance.handle_message(stream, message)
            return
        if message.get("type") == "input":
            if stream in self._streams:
                self._inject(stream, message)
            return
        if message.get("type") == "clipboard":
            if stream in self._streams and self._clipboard is not None:
                self.status.emit(f"Clipboard update received from {_peer(stream.socket)}")
                self._clipboard.apply(message)
            return
        if message.get("type") == "keyframe":
            # The client lost sync with the delta stream (e.g. a band failed
            # to decode) and wants a full frame to rebuild its canvas.
            if stream in self._streams:
                self.status.emit(f"Keyframe requested by {_peer(stream.socket)}")
                self._needs_keyframe.add(stream)
            return
        if (
            message.get("type") != "hello"
            or stream in self._streams
            or stream in self._prompting
        ):
            return
        self._handle_hello(stream, message)

    def revoke_client(self, client_id: str) -> None:
        """Remove a client's pairing and disconnect it if currently connected.

        After this the client must be approved again to reconnect.
        """
        self._paired.revoke(client_id)
        self._revoked.add(client_id)
        self.status.emit(f"Revoked access for client {client_id}")
        connected = [
            stream for stream, (cid, _n, _p) in self._stream_key.items() if cid == client_id
        ]
        for stream in connected:
            stream.send_json({"type": "denied", "reason": "access revoked"})
            stream.socket.disconnectFromHost()  # _drop reports this as "revoked"
        if not connected:
            # Not connected right now: still mark it revoked in the inventory.
            self.peerEvent.emit(
                {"key": client_id, "event": "revoked", "name": "", "address": "", "detail": client_id}
            )

    def _emit_peer(self, stream: MessageStream, event: str) -> None:
        client_id, client_name, peer = self._stream_key.get(
            stream, ("", "", _peer(stream.socket))
        )
        self.peerEvent.emit(
            {
                "key": client_id or peer,
                "event": event,
                "name": client_name,
                "address": peer,
                "detail": client_id,
            }
        )

    def _handle_hello(self, stream: MessageStream, message: dict) -> None:
        peer = _peer(stream.socket)
        client_id = message.get("client_id")
        client_name = str(message.get("name", "unknown"))
        self.status.emit(f'Hello from "{client_name}" ({client_id}) at {peer}')
        if message.get("delta"):
            self._delta_capable.add(stream)
        if isinstance(client_id, str) and client_id:
            self._stream_key[stream] = (client_id, client_name, peer)
            self._emit_peer(stream, "attempt")
        if message.get("version") != PROTOCOL_VERSION:
            self.status.emit(
                f"Denying {peer}: incompatible protocol version {message.get('version')}"
            )
            self._emit_peer(stream, "denied")
            self._final.add(stream)
            stream.send_json({"type": "denied", "reason": "incompatible protocol version"})
            stream.socket.disconnectFromHost()
            return
        if not isinstance(client_id, str) or not client_id:
            self.status.emit(f"Denying {peer}: missing client id")
            stream.socket.abort()
            return

        existing = self._paired.token_for(client_id)
        presented = message.get("token")
        if existing and isinstance(presented, str) and hmac.compare_digest(existing, presented):
            self.status.emit(f'"{client_name}" authenticated with its paired token')
            self._revoked.discard(client_id)
            self._emit_peer(stream, "authenticated")
            self._admit(stream, client_name, {"type": "welcome", "name": socket_module.gethostname()})
            return

        if existing:
            self.status.emit(
                f'"{client_name}" is known but sent no valid token — asking for permission again'
            )
        else:
            self.status.emit(f'"{client_name}" is new — asking for permission')
        # Tell the client it's waiting on a human, not a stalled connection.
        stream.send_json({"type": "pending"})
        # The approval prompt is modal: a nested event loop runs while it is
        # open, so this stream can disconnect (and _drop can run) meanwhile.
        self._prompting.add(stream)
        try:
            approved = self._approve_client(client_id, client_name)
        finally:
            self._prompting.discard(stream)
        if stream not in self._all_streams:
            self.status.emit(f'"{client_name}" disconnected while waiting for permission')
            return
        if not approved:
            self.status.emit(f'Permission for "{client_name}" refused — denying')
            self._emit_peer(stream, "refused")
            self._final.add(stream)
            stream.send_json({"type": "denied", "reason": "connection refused by user"})
            stream.socket.disconnectFromHost()
            return
        # Reissue the existing token if any (so the client re-stores it); else pair anew.
        token = existing or self._paired.pair(client_id)
        self._revoked.discard(client_id)
        self.status.emit(f'Permission granted — "{client_name}" paired')
        self._emit_peer(stream, "paired")
        self._admit(
            stream,
            client_name,
            {"type": "welcome", "name": socket_module.gethostname(), "token": token},
        )

    def _admit(self, stream: MessageStream, client_name: str, welcome: dict) -> None:
        stream.max_payload = MAX_PAYLOAD  # pre-auth cap lifted once approved
        stream.send_json(welcome)
        self._needs_keyframe.add(stream)  # its first frame must be a full one
        self._streams.append(stream)
        if self._performance is not None:
            self._performance.add_stream(stream)
        self.clientCountChanged.emit(len(self._streams))
        self.status.emit(
            f'Streaming screen to "{client_name}" at {self._fps} fps '
            f"({len(self._streams)} viewer(s) total)"
        )
        if not self._timer.isActive():
            self._timer.start()

    def _inject(self, stream: MessageStream, message: dict) -> None:
        action = message.get("action")
        x, y = message.get("x"), message.get("y")
        if stream not in self._controllers:
            self._controllers.add(stream)
            if not self._injector.available:
                self.status.emit(
                    "Receiving remote input, but injection is unavailable on this platform"
                )
            else:
                self.status.emit(f"Remote input control started from {_peer(stream.socket)}")
        try:
            match action:
                case "move":
                    self._injector.move(_coord(x), _coord(y))
                case "button":
                    pressed = bool(message.get("pressed"))
                    name = str(message.get("button"))
                    self._injector.button(x, y, name, pressed)
                    buttons, _keys = self._pressed.setdefault(stream, (set(), set()))
                    (buttons.add if pressed else buttons.discard)(name)
                    self.status.emit(f"Injected {name} button {'down' if pressed else 'up'}")
                case "wheel":
                    self._injector.wheel(_coord(x), _coord(y), int(message.get("dy", 0)))
                case "key":
                    pressed = bool(message.get("pressed"))
                    vk = int(message.get("vk", 0))
                    self._injector.key(vk, pressed)
                    _buttons, keys = self._pressed.setdefault(stream, (set(), set()))
                    (keys.add if pressed else keys.discard)(vk)
                    self.status.emit(f"Injected key vk={vk} {'down' if pressed else 'up'}")
        except (TypeError, ValueError) as error:
            self.status.emit(f"Ignoring malformed input message: {error}")

    def _release_input(self, stream: MessageStream) -> None:
        """Release whatever the stream still holds down, so a client that
        vanishes mid-drag or mid-keystroke doesn't leave input stuck."""
        buttons, keys = self._pressed.pop(stream, (set(), set()))
        for name in sorted(buttons):
            # No coordinates: release at the cursor's current position.
            self._injector.button(None, None, name, False)
        for vk in sorted(keys):
            self._injector.key(vk, False)
        if buttons or keys:
            self.status.emit(
                f"Released {len(buttons)} mouse button(s) and {len(keys)} key(s) "
                "still held by the disconnected client"
            )

    def _broadcast_clipboard(self, payload: dict) -> None:
        if not self._streams:
            return
        self.status.emit(f"Sending clipboard update to {len(self._streams)} viewer(s)")
        message = {"type": "clipboard", **payload}
        for stream in self._streams:
            stream.send_json(message)

    def _drop(self, stream: MessageStream) -> None:
        # Detach from the monitor before the deferred delete below, so it
        # never samples a deleted stream (idempotent for never-admitted ones).
        if self._performance is not None:
            self._performance.remove_stream(stream)
        self._controllers.discard(stream)
        self._all_streams.discard(stream)
        self._prompting.discard(stream)
        self._backlogged.discard(stream)
        self._delta_capable.discard(stream)
        self._needs_keyframe.discard(stream)
        self._release_input(stream)
        final = stream in self._final
        self._final.discard(stream)
        if stream in self._stream_key:
            client_id = self._stream_key[stream][0]
            if not final:  # a terminal state (denied/refused) is already recorded
                self._emit_peer(
                    stream, "revoked" if client_id in self._revoked else "disconnected"
                )
            del self._stream_key[stream]
        # Fully reset the socket before the deferred delete: destroying a
        # QSslSocket with TLS teardown still in flight crashes intermittently.
        stream.socket.blockSignals(True)
        stream.socket.abort()
        stream.socket.deleteLater()
        stream.deleteLater()
        if stream not in self._streams:
            self.status.emit("Connection closed before completing hello")
            return
        self._streams.remove(stream)
        self.clientCountChanged.emit(len(self._streams))
        self.status.emit(f"Client disconnected — {len(self._streams)} viewer(s) remaining")
        if not self._streams:
            self._timer.stop()
            self._previous_frame = None  # don't hold a stale capture
            self.status.emit("No viewers left — screen capture stopped")

    def _capture(self) -> QImage | None:
        """Grab the primary screen at full resolution (tests override this
        to drive deterministic frame content)."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        image = screen.grabWindow(0).toImage()
        return None if image.isNull() else image

    def _broadcast_frame(self) -> None:
        if not self._streams:
            return
        image = self._capture()
        if image is None:
            self.status.emit("Screen capture failed (null image)")
            return
        # bands is None when there is no comparable previous capture (first
        # frame, or the resolution changed): everyone gets a full frame.
        bands = (
            frames.changed_bands(self._previous_frame, image)
            if self._previous_frame is not None
            else None
        )
        self._previous_frame = image
        # Encode each variant at most once per tick, shared by all takers.
        legacy_jpeg: bytes | None = None
        keyframe_png: bytes | None = None
        delta_payload: bytes | None = None
        for stream in self._streams:
            if stream.socket.bytesToWrite() > _MAX_SEND_BACKLOG:
                # Client is not keeping up; drop frames for it (and say so —
                # to the viewer this looks like a frozen or flaky connection).
                # Its canvas will be stale once it catches up, so it must
                # restart from a keyframe.
                self._needs_keyframe.add(stream)
                if stream not in self._backlogged:
                    self._backlogged.add(stream)
                    self.status.emit(
                        f"Viewer at {_peer(stream.socket)} is not keeping up "
                        f"({stream.socket.bytesToWrite() // 1024} KB unsent) — "
                        "dropping frames for it"
                    )
                continue
            if stream in self._backlogged:
                self._backlogged.discard(stream)
                self.status.emit(
                    f"Viewer at {_peer(stream.socket)} caught up — resuming frames"
                )
            if stream not in self._delta_capable:
                # Legacy (0.5.0) client: full JPEG every tick, as before.
                if legacy_jpeg is None:
                    legacy_jpeg = frames.encode_image(image, "JPEG", JPEG_QUALITY)
                stream.send_frame(legacy_jpeg)
            elif stream in self._needs_keyframe or bands is None:
                if keyframe_png is None:
                    keyframe_png = frames.encode_image(image)
                    _log.debug("Keyframe: %d KB PNG", len(keyframe_png) // 1024)
                stream.send_frame(keyframe_png)
                self._needs_keyframe.discard(stream)
            elif bands:
                if delta_payload is None:
                    delta_payload = frames.encode_delta(image, bands)
                stream.send_delta(delta_payload)
            # else: nothing changed since the last frame — send nothing.


class ShareClient(QObject):
    """Connects to a ShareServer over TLS, authenticates, and emits frames."""

    connected = Signal(str)  # server name
    approvalPending = Signal()  # server is asking its user for permission
    denied = Signal(str)  # reason
    disconnected = Signal()
    frameReceived = Signal(QImage)
    status = Signal(str)

    def __init__(
        self,
        identity: tuple[str, str] | None = None,
        *,
        known_servers: KnownServers | None = None,
        clipboard=None,
        performance: PerformanceMonitor | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._performance = performance
        self._client_id, self._name = identity or load_client_identity(
            db.connect(default_db_path())
        )
        self._known = known_servers
        self._got_first_frame = False
        self._clipboard = clipboard
        if clipboard is not None:
            clipboard.changed.connect(self._send_clipboard)
        self._server_key = ""
        self._server_fingerprint = ""
        self._server_token: str | None = None
        self._frame_count = 0
        self._last_image: QImage | None = None  # delta patches build on this
        self._socket = QSslSocket(self)
        self._socket.setSslConfiguration(tls.client_configuration())
        self._stream = MessageStream(self._socket, self)
        self._socket.stateChanged.connect(
            lambda state: _log.debug("Client socket state: %s", state.name)
        )
        self._socket.encrypted.connect(self._on_encrypted)
        self._socket.sslErrors.connect(self._on_ssl_errors)
        self._socket.disconnected.connect(self._on_disconnected)
        self._socket.errorOccurred.connect(
            lambda _error: self.status.emit(f"Socket error: {self._socket.errorString()}")
        )
        self._stream.jsonReceived.connect(self._on_message)
        self._stream.frameReceived.connect(self._on_frame)
        self._stream.deltaReceived.connect(self._on_delta)

    def connect_to(self, host: str, port: int) -> None:
        self._socket.abort()  # drop any previous connection or attempt
        if self._performance is not None:
            # abort() emits no disconnected signal, so detach explicitly.
            self._performance.remove_stream(self._stream)
        self._got_first_frame = False
        self._frame_count = 0
        self._last_image = None
        self._server_key = f"{host}:{port}"
        record = self._known.get(self._server_key) if self._known else None
        self._server_token = record.get("token") if record else None
        self.status.emit(f"Connecting to {host}:{port} over TLS …")
        self._socket.connectToHostEncrypted(host, port)

    def close(self) -> None:
        self._socket.abort()

    def send_input(self, event: dict) -> None:
        if self._socket.state() == QSslSocket.SocketState.ConnectedState:
            self._stream.send_json({"type": "input", **event})

    def _send_clipboard(self, payload: dict) -> None:
        if self._socket.state() == QSslSocket.SocketState.ConnectedState:
            self.status.emit("Sending local clipboard to server")
            self._stream.send_json({"type": "clipboard", **payload})

    def _on_ssl_errors(self, errors) -> None:
        # Self-signed server certificate is expected; identity is pinned instead.
        self.status.emit(
            "Ignoring expected TLS certificate warnings: "
            + "; ".join(e.errorString() for e in errors)
        )
        self._socket.ignoreSslErrors()

    def _on_encrypted(self) -> None:
        # TCP_NODELAY (settable only once connected): keystrokes are tiny
        # messages; Nagle + delayed ACK would add tens to hundreds of ms.
        self._socket.setSocketOption(QAbstractSocket.SocketOption.LowDelayOption, 1)
        cert = self._socket.peerCertificate()
        fingerprint = "" if cert.isNull() else tls.certificate_fingerprint(cert)
        self._server_fingerprint = fingerprint
        record = self._known.get(self._server_key) if self._known else None
        if record and record.get("fingerprint") and record["fingerprint"] != fingerprint:
            self.status.emit(
                "WARNING: server certificate fingerprint changed since last pairing "
                "(continuing anyway)"
            )
        self.status.emit(f"TLS established (server cert {fingerprint[:16]}…) — sending hello")
        hello = {
            "type": "hello",
            "version": PROTOCOL_VERSION,
            "client_id": self._client_id,
            "name": self._name,
            "delta": True,  # we understand inter-frame delta messages
        }
        if self._server_token:
            hello["token"] = self._server_token
        self._stream.send_json(hello)

    def _on_disconnected(self) -> None:
        if self._performance is not None:
            self._performance.remove_stream(self._stream)
        self.status.emit("Disconnected from server")
        self.disconnected.emit()

    def _on_message(self, message: dict) -> None:
        match message.get("type"):
            case "welcome":
                name = str(message.get("name", ""))
                token = message.get("token")
                if self._known is not None:
                    if isinstance(token, str):
                        self._known.remember(self._server_key, self._server_fingerprint, token)
                        self.status.emit("Paired with server — token stored for future connections")
                    else:
                        # Token reconnect: refresh the pinned fingerprint if the
                        # server's certificate changed, so the change warning
                        # doesn't repeat on every future connection.
                        record = self._known.get(self._server_key)
                        if record and record.get("fingerprint") != self._server_fingerprint:
                            self._known.remember(
                                self._server_key, self._server_fingerprint, record["token"]
                            )
                            self.status.emit("Stored server certificate fingerprint updated")
                self.status.emit(
                    f'Server "{name}" accepted the connection — waiting for first frame'
                )
                self.connected.emit(name)
                if self._performance is not None:
                    # Attach only once admitted: handshake traffic is never
                    # sampled and no ping goes to a server that hasn't
                    # welcomed us.
                    self._performance.add_stream(self._stream)
            case "pending":
                self.status.emit(
                    "Server is asking its user for permission — waiting for approval"
                )
                self.approvalPending.emit()
            case "denied":
                reason = str(message.get("reason", "denied"))
                self.status.emit(f"Server denied the connection: {reason}")
                self.denied.emit(reason)
            case "clipboard":
                if self._clipboard is not None:
                    self.status.emit("Clipboard update received from server")
                    self._clipboard.apply(message)
            case "ping" | "pong":
                if self._performance is not None:
                    self._performance.handle_message(self._stream, message)

    def _on_frame(self, data: bytes) -> None:
        # Full frame — PNG keyframe or (legacy servers) JPEG; sniffed from
        # the payload's magic bytes.
        image = QImage.fromData(data)
        if image.isNull():
            self.status.emit(f"Received undecodable frame ({len(data)} bytes)")
            return
        self._deliver(image, len(data))

    def _on_delta(self, payload: bytes) -> None:
        if self._last_image is None:
            self.status.emit("Delta frame arrived before a keyframe — requesting one")
            self._stream.send_json({"type": "keyframe"})
            return
        image = frames.apply_delta(self._last_image, payload)
        if image is None:
            self.status.emit("Undecodable delta frame — requesting a keyframe")
            self._stream.send_json({"type": "keyframe"})
            return
        self._deliver(image, len(payload))

    def _deliver(self, image: QImage, byte_count: int) -> None:
        self._last_image = image
        if not self._got_first_frame:
            self._got_first_frame = True
            self.status.emit(
                f"First frame received: {image.width()}x{image.height()} "
                f"({byte_count // 1024} KB)"
            )
        self._frame_count += 1
        # A heartbeat in the debug log (~10 s at the default fps): gaps between
        # these lines show exactly when the stream stalled.
        if self._frame_count % 100 == 0:
            _log.debug(
                "Received %d frames (latest %dx%d, %d KB)",
                self._frame_count,
                image.width(),
                image.height(),
                byte_count // 1024,
            )
        self.frameReceived.emit(image)
