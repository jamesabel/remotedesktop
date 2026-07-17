import time

import pytest
from PySide6.QtCore import QEventLoop

from remotedesktop.config import KnownServers, PairedClients
from remotedesktop.sharing import ShareClient, ShareServer

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
IDENTITY = (CLIENT_ID, "test-client")


def pump(qapp, condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met while pumping events")
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)


def make_server(credentials, tmp_path, *, approve, injector=None, clipboard=None):
    server = ShareServer(
        approve_client=approve,
        credentials=credentials,
        paired=PairedClients(tmp_path / "paired.json"),
        injector=injector,
        clipboard=clipboard,
    )
    assert server.listen(0)
    return server


def make_client(tmp_path, *, clipboard=None):
    return ShareClient(
        identity=IDENTITY,
        known_servers=KnownServers(tmp_path / "known.json"),
        clipboard=clipboard,
    )


def test_first_connection_prompts_pairs_and_streams(qapp, credentials, tmp_path):
    prompts = []
    server = make_server(
        credentials, tmp_path, approve=lambda cid, name: prompts.append((cid, name)) or True
    )
    client = make_client(tmp_path)
    frames, names = [], []
    client.frameReceived.connect(frames.append)
    client.connected.connect(names.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: frames)
        assert prompts == [IDENTITY]
        assert names and names[0]
        assert frames[0].width() > 0
        # The server issued a token and the client stored it.
        assert CLIENT_ID in PairedClients(tmp_path / "paired.json")
        assert KnownServers(tmp_path / "known.json").get(f"127.0.0.1:{server.port}")["token"]
    finally:
        client.close()
        server.close()


def test_reconnect_uses_token_without_prompting(qapp, credentials, tmp_path):
    prompts = []
    # Keep one server (same port) so the client's stored "host:port" token matches.
    server = make_server(
        credentials, tmp_path, approve=lambda cid, name: prompts.append(cid) or True
    )
    server_statuses: list[str] = []
    server.status.connect(server_statuses.append)
    try:
        # First connection: prompts and pairs.
        client = make_client(tmp_path)
        names = []
        client.connected.connect(names.append)
        client.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: names)
        client.close()
        pump(qapp, lambda: True, timeout=0.3)

        # Second connection: authenticates by token, no second prompt.
        client2 = make_client(tmp_path)
        names2 = []
        client2.connected.connect(names2.append)
        client2.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: names2)
        assert len(prompts) == 1  # only the first connection prompted
        assert any("authenticated with its paired token" in s for s in server_statuses)
    finally:
        client2.close()
        client.close()
        server.close()


def test_refused_client_is_denied_and_not_paired(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: False)
    client = make_client(tmp_path)
    denials = []
    client.denied.connect(denials.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: denials)
        assert "refused" in denials[0]
        assert CLIENT_ID not in PairedClients(tmp_path / "paired.json")
    finally:
        client.close()
        server.close()


def test_server_reports_phases_in_status(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    statuses: list[str] = []
    server.status.connect(statuses.append)
    client = make_client(tmp_path)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        text = "\n".join(statuses)
        assert "Incoming TLS connection" in text
        assert 'Hello from "test-client"' in text
        assert "paired" in text
        assert "Streaming screen" in text
    finally:
        client.close()
        server.close()
