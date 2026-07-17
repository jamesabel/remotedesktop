import json
import socket

import pytest

from remotedesktop.discovery import DiscoveryResponder, ServerInfo, discover_servers

LOOPBACK = "127.0.0.1"


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


@pytest.fixture
def responder_port():
    port = free_udp_port()
    responder = DiscoveryResponder(
        "testbox", 12345, discovery_port=port, bind_host=LOOPBACK
    )
    responder.start()
    yield port
    responder.stop()


def test_discover_finds_server(responder_port) -> None:
    servers = discover_servers(
        timeout=2.0, discovery_port=responder_port, broadcast_hosts=(LOOPBACK,)
    )
    assert servers == [ServerInfo(name="testbox", host=LOOPBACK, port=12345)]


def test_discover_times_out_when_no_server() -> None:
    servers = discover_servers(
        timeout=0.2, discovery_port=free_udp_port(), broadcast_hosts=(LOOPBACK,)
    )
    assert servers == []


def test_responder_ignores_invalid_datagrams(responder_port) -> None:
    valid_probe = json.dumps(
        {"magic": "remotedesktop", "version": 1, "type": "probe"}
    ).encode()
    invalid = [
        b"\xff\xfenot json",
        b'"just a string"',
        json.dumps({"magic": "wrong", "version": 1, "type": "probe"}).encode(),
        json.dumps({"magic": "remotedesktop", "version": 99, "type": "probe"}).encode(),
        json.dumps({"magic": "remotedesktop", "version": 1, "type": "reply"}).encode(),
    ]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((LOOPBACK, 0))
        for datagram in invalid:
            sock.sendto(datagram, (LOOPBACK, responder_port))
        sock.sendto(valid_probe, (LOOPBACK, responder_port))
        sock.settimeout(2.0)
        reply = json.loads(sock.recvfrom(4096)[0])
        assert reply["type"] == "reply"
        assert reply["name"] == "testbox"
        # Only the valid probe got a reply; the socket has nothing further.
        sock.settimeout(0.2)
        with pytest.raises(TimeoutError):
            sock.recvfrom(4096)


def test_responder_stop_is_idempotent() -> None:
    responder = DiscoveryResponder(
        "testbox", 12345, discovery_port=free_udp_port(), bind_host=LOOPBACK
    )
    responder.start()
    responder.stop()
    responder.stop()
