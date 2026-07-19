import json
import socket
import threading

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


def test_responder_start_twice_raises(responder_port) -> None:
    responder = DiscoveryResponder(
        "other", 12345, discovery_port=free_udp_port(), bind_host=LOOPBACK
    )
    responder.start()
    try:
        with pytest.raises(RuntimeError):
            responder.start()
    finally:
        responder.stop()


def test_responder_bind_conflict_raises_and_cleans_up(responder_port) -> None:
    # responder_port is already bound by the fixture's responder.
    responder = DiscoveryResponder(
        "other", 12345, discovery_port=responder_port, bind_host=LOOPBACK
    )
    with pytest.raises(OSError):
        responder.start()
    assert responder._socket is None
    responder.stop()  # must be safe after a failed start


def test_replies_with_malformed_fields_are_ignored() -> None:
    port = free_udp_port()

    def fake_server(sock: socket.socket) -> None:
        _probe, sender = sock.recvfrom(4096)
        bad = {"magic": "remotedesktop", "version": 1, "type": "reply", "name": 7, "port": "x"}
        good = {"magic": "remotedesktop", "version": 1, "type": "reply", "name": "ok", "port": 9}
        sock.sendto(json.dumps(bad).encode(), sender)
        sock.sendto(json.dumps(good).encode(), sender)
        sock.sendto(json.dumps(good).encode(), sender)  # duplicate is deduplicated

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((LOOPBACK, port))
        thread = threading.Thread(target=fake_server, args=(sock,), daemon=True)
        thread.start()
        servers = discover_servers(
            timeout=1.0, discovery_port=port, broadcast_hosts=(LOOPBACK,)
        )
        thread.join()
    assert servers == [ServerInfo(name="ok", host=LOOPBACK, port=9)]


def test_probe_send_failure_is_tolerated() -> None:
    servers = discover_servers(
        timeout=0.1,
        discovery_port=free_udp_port(),
        broadcast_hosts=("256.256.256.256",),  # unroutable: sendto raises OSError
    )
    assert servers == []


def test_probes_are_resent_so_a_late_responder_is_still_found() -> None:
    # The responder starts mid-scan: only a re-sent probe can reach it. A
    # single-probe scan (the old behavior) would find nothing here.
    port = free_udp_port()
    responder = DiscoveryResponder("latebox", 4242, discovery_port=port, bind_host=LOOPBACK)
    timer = threading.Timer(0.5, responder.start)
    timer.start()
    try:
        servers = discover_servers(
            timeout=2.0, discovery_port=port, broadcast_hosts=(LOOPBACK,)
        )
        assert servers == [ServerInfo(name="latebox", host=LOOPBACK, port=4242)]
    finally:
        timer.cancel()
        responder.stop()
