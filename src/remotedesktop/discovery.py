"""LAN autodiscovery.

Clients broadcast a JSON probe datagram to the discovery port; every server
on the LAN replies with its display name and TCP connection port. Datagrams
whose magic, protocol version, or type don't match are ignored.
"""

import json
import socket
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

DISCOVERY_PORT = 48653
DEFAULT_CONNECT_PORT = 48654

_MAGIC = "remotedesktop"
_PROTOCOL_VERSION = 1
_MAX_DATAGRAM = 4096


@dataclass(frozen=True)
class ServerInfo:
    name: str
    host: str
    port: int


def _parse(data: bytes, expected_type: str) -> dict | None:
    try:
        message = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict):
        return None
    if message.get("magic") != _MAGIC or message.get("version") != _PROTOCOL_VERSION:
        return None
    if message.get("type") != expected_type:
        return None
    return message


def _encode(message_type: str, **fields: object) -> bytes:
    return json.dumps(
        {"magic": _MAGIC, "version": _PROTOCOL_VERSION, "type": message_type, **fields}
    ).encode()


class DiscoveryResponder:
    """Runs on the server; answers discovery probes with this server's info."""

    def __init__(
        self,
        name: str,
        connect_port: int,
        *,
        discovery_port: int = DISCOVERY_PORT,
        bind_host: str = "",
    ) -> None:
        self._reply = _encode("reply", name=name, port=connect_port)
        self._discovery_port = discovery_port
        self._bind_host = bind_host
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("responder already started")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self._bind_host, self._discovery_port))
        sock.settimeout(0.25)
        self._socket = sock
        self._running.set()
        self._thread = threading.Thread(
            target=self._serve, name="discovery-responder", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _serve(self) -> None:
        assert self._socket is not None
        while self._running.is_set():
            try:
                data, sender = self._socket.recvfrom(_MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError:
                return
            if _parse(data, "probe") is None:
                continue
            try:
                self._socket.sendto(self._reply, sender)
            except OSError:
                pass


def discover_servers(
    timeout: float = 1.0,
    *,
    discovery_port: int = DISCOVERY_PORT,
    broadcast_hosts: Sequence[str] = ("255.255.255.255",),
) -> list[ServerInfo]:
    """Broadcast a probe and collect server replies until the timeout expires.

    Results are deduplicated by (host, port); order is arrival order.
    """
    found: dict[tuple[str, int], ServerInfo] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        probe = _encode("probe")
        for host in broadcast_hosts:
            try:
                sock.sendto(probe, (host, discovery_port))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                data, (host, _sender_port) = sock.recvfrom(_MAX_DATAGRAM)
            except (TimeoutError, OSError):
                break
            message = _parse(data, "reply")
            if message is None:
                continue
            name = message.get("name")
            port = message.get("port")
            if not isinstance(name, str) or not isinstance(port, int):
                continue
            found.setdefault((host, port), ServerInfo(name=name, host=host, port=port))
    return list(found.values())
