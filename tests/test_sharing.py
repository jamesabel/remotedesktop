import time

from PySide6.QtCore import QEventLoop

from remotedesktop import db, tls
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
    # The server is a distinct "machine" from the client -> its own database.
    server = ShareServer(
        approve_client=approve,
        credentials=credentials,
        paired=PairedClients(db.connect(tmp_path / "server.db")),
        injector=injector,
        clipboard=clipboard,
    )
    assert server.listen(0)
    return server


def make_client(tmp_path, *, clipboard=None):
    return ShareClient(
        identity=IDENTITY,
        known_servers=KnownServers(db.connect(tmp_path / "client.db")),
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
        assert CLIENT_ID in PairedClients(db.connect(tmp_path / "server.db"))
        record = KnownServers(db.connect(tmp_path / "client.db")).get(
            f"127.0.0.1:{server.port}"
        )
        assert record is not None and record["token"]
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


def test_revoke_disconnects_and_requires_reapproval(qapp, credentials, tmp_path):
    prompts = []
    server = make_server(
        credentials, tmp_path, approve=lambda cid, name: prompts.append(cid) or True
    )
    try:
        # First connection pairs.
        client = make_client(tmp_path)
        names = []
        client.connected.connect(names.append)
        client.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: names)
        assert CLIENT_ID in PairedClients(db.connect(tmp_path / "server.db"))

        # Revoke: the client is disconnected and the token removed.
        disconnected = []
        client.disconnected.connect(lambda: disconnected.append(True))
        server.revoke_client(CLIENT_ID)
        pump(qapp, lambda: disconnected)
        assert CLIENT_ID not in PairedClients(db.connect(tmp_path / "server.db"))

        # Reconnecting now prompts for approval again (its token no longer works).
        client2 = make_client(tmp_path)
        names2 = []
        client2.connected.connect(names2.append)
        client2.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: names2)
        assert len(prompts) == 2
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
        assert CLIENT_ID not in PairedClients(db.connect(tmp_path / "server.db"))
    finally:
        client.close()
        server.close()


def test_disconnect_during_approval_is_not_admitted(qapp, credentials, tmp_path):
    client = make_client(tmp_path)

    def approve(cid, name):
        # Simulate the user answering the (modal, nested-event-loop) prompt
        # only after the client already gave up and disconnected.
        client.close()
        pump(qapp, lambda: not server._all_streams)
        return True

    server = make_server(credentials, tmp_path, approve=approve)
    counts, statuses = [], []
    server.clientCountChanged.connect(counts.append)
    server.status.connect(statuses.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: any("disconnected while waiting" in s for s in statuses))
        assert counts == []  # the dead stream was never admitted
        assert server._streams == []
        assert not server._timer.isActive()
    finally:
        client.close()
        server.close()


def test_reapproved_client_disconnect_is_not_reported_revoked(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    events = []
    server.peerEvent.connect(lambda e: events.append(e["event"]))
    client = client2 = None
    try:
        client = make_client(tmp_path)
        names = []
        client.connected.connect(names.append)
        client.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: names)

        server.revoke_client(CLIENT_ID)
        pump(qapp, lambda: "revoked" in events)

        # Re-approve: a later ordinary disconnect must not read "revoked".
        client2 = make_client(tmp_path)
        names2 = []
        client2.connected.connect(names2.append)
        client2.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: names2)
        client2.close()
        pump(qapp, lambda: events[-1] in ("disconnected", "revoked"))
        assert events[-1] == "disconnected"
    finally:
        if client2 is not None:
            client2.close()
        if client is not None:
            client.close()
        server.close()


def test_refused_state_is_not_overwritten_by_disconnect(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: False)
    events = []
    server.peerEvent.connect(lambda e: events.append(e["event"]))
    client = make_client(tmp_path)
    denials = []
    client.denied.connect(denials.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: denials)
        pump(qapp, lambda: not server._all_streams)  # server processed the drop
        assert events == ["attempt", "refused"]
    finally:
        client.close()
        server.close()


def test_stored_fingerprint_updates_when_server_cert_changes(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    port = server.port
    client = make_client(tmp_path)
    names = []
    client.connected.connect(names.append)
    client.connect_to("127.0.0.1", port)
    pump(qapp, lambda: names)
    client.close()
    server.close()
    pump(qapp, lambda: True, timeout=0.3)

    # Same server database (the token survives) but a brand-new certificate.
    new_credentials = tls.ephemeral_credentials()
    prompts = []
    server2 = ShareServer(
        approve_client=lambda *_: prompts.append(True) or True,
        credentials=new_credentials,
        paired=PairedClients(db.connect(tmp_path / "server.db")),
    )
    assert server2.listen(port)
    client2 = make_client(tmp_path)
    names2 = []
    client2.connected.connect(names2.append)
    client2.connect_to("127.0.0.1", port)
    try:
        pump(qapp, lambda: names2)
        assert prompts == []  # the paired token still authenticates
        stored = KnownServers(db.connect(tmp_path / "client.db")).get(f"127.0.0.1:{port}")
        assert stored is not None
        assert stored["fingerprint"] == tls.certificate_fingerprint(new_credentials[0])
    finally:
        client2.close()
        server2.close()


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
