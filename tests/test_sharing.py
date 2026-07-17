import logging
import time

from PySide6.QtCore import QEventLoop
from PySide6.QtNetwork import QSslSocket

from remotedesktop import db, tls
from remotedesktop.config import KnownServers, PairedClients
from remotedesktop.protocol import PROTOCOL_VERSION, MessageStream
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


def test_approval_pending_is_signaled_only_when_prompting(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    try:
        # First connection: the server prompts, so the client hears "pending"
        # before it is admitted.
        client = make_client(tmp_path)
        events: list[str] = []
        client.approvalPending.connect(lambda: events.append("pending"))
        client.connected.connect(lambda _name: events.append("connected"))
        client.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: "connected" in events)
        assert events == ["pending", "connected"]
        client.close()
        pump(qapp, lambda: True, timeout=0.3)

        # Token reconnect: no prompt on the server, so no pending signal.
        client2 = make_client(tmp_path)
        events2: list[str] = []
        client2.approvalPending.connect(lambda: events2.append("pending"))
        client2.connected.connect(lambda _name: events2.append("connected"))
        client2.connect_to("127.0.0.1", server.port)
        pump(qapp, lambda: "connected" in events2)
        assert events2 == ["connected"]
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


def raw_tls_stream(qapp, port):
    """A protocol-level client: TLS socket + MessageStream, no ShareClient."""
    sock = QSslSocket()
    sock.setSslConfiguration(tls.client_configuration())
    sock.sslErrors.connect(lambda _errors: sock.ignoreSslErrors())
    stream = MessageStream(sock)
    sock.connectToHostEncrypted("127.0.0.1", port)
    pump(qapp, lambda: sock.isEncrypted())
    return sock, stream


def test_incompatible_protocol_version_is_denied(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    events = []
    server.peerEvent.connect(lambda e: events.append(e["event"]))
    sock, stream = raw_tls_stream(qapp, server.port)
    replies = []
    stream.jsonReceived.connect(replies.append)
    try:
        stream.send_json(
            {"type": "hello", "version": 999, "client_id": "cid-1", "name": "old-client"}
        )
        pump(qapp, lambda: replies)
        assert replies[0] == {"type": "denied", "reason": "incompatible protocol version"}
        assert "denied" in events
    finally:
        sock.abort()
        server.close()


def test_hello_without_client_id_is_aborted(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    statuses = []
    server.status.connect(statuses.append)
    sock, stream = raw_tls_stream(qapp, server.port)
    try:
        stream.send_json({"type": "hello", "version": PROTOCOL_VERSION, "name": "anon"})
        pump(qapp, lambda: sock.state() != QSslSocket.SocketState.ConnectedState)
        assert any("missing client id" in s for s in statuses)
    finally:
        sock.abort()
        server.close()


def test_malformed_input_messages_are_ignored(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    statuses = []
    server.status.connect(statuses.append)
    client = make_client(tmp_path)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        client.send_input({"action": "move"})  # no coordinates
        client.send_input({"action": "wheel", "x": "bogus", "y": 0.5, "dy": "?"})
        pump(qapp, lambda: sum("Ignoring malformed input" in s for s in statuses) >= 2)
    finally:
        client.close()
        server.close()


def test_listen_fails_on_occupied_port(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    other = ShareServer(
        approve_client=lambda *_: True,
        credentials=credentials,
        paired=PairedClients(db.connect(tmp_path / "other.db")),
    )
    statuses = []
    other.status.connect(statuses.append)
    try:
        assert not other.listen(server.port)
        assert any("Cannot listen" in s for s in statuses)
    finally:
        other.close()
        server.close()


def test_revoking_a_disconnected_client_still_marks_inventory(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    events = []
    server.peerEvent.connect(events.append)
    try:
        server.revoke_client("ghost-client")
        assert events[-1]["event"] == "revoked"
        assert events[-1]["key"] == "ghost-client"
    finally:
        server.close()


class _FakeSslError:
    @staticmethod
    def errorString() -> str:
        return "self-signed certificate"


def test_disconnected_client_paths_are_noops(qapp, tmp_path):
    client = make_client(tmp_path)
    statuses = []
    client.status.connect(statuses.append)
    client.send_input({"action": "move", "x": 0.5, "y": 0.5})  # not connected
    client._send_clipboard({"text": "x"})  # not connected
    client._on_frame(b"definitely not a jpeg")
    assert any("undecodable frame" in s for s in statuses)
    client._on_ssl_errors([_FakeSslError()])
    assert any("Ignoring expected TLS" in s for s in statuses)
    client.close()


def test_backlogged_viewer_frame_drop_is_reported(qapp, credentials, tmp_path, monkeypatch):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    statuses: list[str] = []
    server.status.connect(statuses.append)
    client = make_client(tmp_path)
    frames = []
    client.frameReceived.connect(frames.append)
    # A negative cap makes every stream count as backlogged, so no frames go out.
    monkeypatch.setattr("remotedesktop.sharing._MAX_SEND_BACKLOG", -1)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: any("not keeping up" in s for s in statuses))
        assert sum("not keeping up" in s for s in statuses) == 1  # reported once, not per frame
        assert not frames
        # Once the backlog clears the server says so and frames resume.
        monkeypatch.setattr("remotedesktop.sharing._MAX_SEND_BACKLOG", 8 * 1024 * 1024)
        pump(qapp, lambda: frames)
        assert any("caught up" in s for s in statuses)
    finally:
        client.close()
        server.close()


def test_oversized_preauth_message_aborts_and_logs(qapp, credentials, tmp_path, caplog):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    sock, stream = raw_tls_stream(qapp, server.port)
    caplog.set_level(logging.DEBUG, logger="remotedesktop")
    try:
        # Larger than the pre-auth 64 KB cap: the server aborts the socket
        # and the reason must land in the debug log.
        stream.send_frame(b"x" * (128 * 1024))
        pump(qapp, lambda: sock.state() != QSslSocket.SocketState.ConnectedState)
        assert any("exceeds the" in r.getMessage() for r in caplog.records)
    finally:
        sock.abort()
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
