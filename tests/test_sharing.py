import time

import pytest
from PySide6.QtCore import QEventLoop

from remotedesktop.config import ApprovedClients
from remotedesktop.sharing import ShareClient, ShareServer

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
IDENTITY = (CLIENT_ID, "test-client")


def pump(qapp, condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met while pumping events")
        qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)


@pytest.fixture
def approved(tmp_path):
    approved = ApprovedClients(tmp_path / "approved.json")
    approved.add(CLIENT_ID)
    return approved


def make_client(statuses):
    client = ShareClient(identity=IDENTITY)
    client.status.connect(statuses.append)
    return client


def test_approved_client_receives_frames(qapp, approved):
    server = ShareServer(
        approve_client=lambda *_: pytest.fail("approved client must not prompt"),
        approved=approved,
    )
    assert server.listen(0)
    statuses: list[str] = []
    frames = []
    server_names = []
    client = make_client(statuses)
    client.connected.connect(server_names.append)
    client.frameReceived.connect(frames.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: frames)
        assert server_names and server_names[0]
        assert frames[0].width() > 0 and frames[0].height() > 0
        assert any("First frame received" in s for s in statuses)
    finally:
        client.close()
        server.close()


def test_unknown_client_prompts_and_approval_persists(qapp, tmp_path):
    approved_path = tmp_path / "approved.json"
    prompts = []

    def approve(client_id, client_name):
        prompts.append((client_id, client_name))
        return True

    server = ShareServer(approve_client=approve, approved=ApprovedClients(approved_path))
    assert server.listen(0)
    connected = []
    client = make_client([])
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        assert prompts == [IDENTITY]
        assert CLIENT_ID in ApprovedClients(approved_path)
    finally:
        client.close()
        server.close()


def test_refused_client_is_denied_and_not_persisted(qapp, tmp_path):
    approved_path = tmp_path / "approved.json"
    server = ShareServer(
        approve_client=lambda *_: False, approved=ApprovedClients(approved_path)
    )
    assert server.listen(0)
    denials = []
    client = make_client([])
    client.denied.connect(denials.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: denials)
        assert "refused" in denials[0]
        assert CLIENT_ID not in ApprovedClients(approved_path)
    finally:
        client.close()
        server.close()


def test_server_reports_phases_in_status(qapp, approved):
    server = ShareServer(approve_client=lambda *_: False, approved=approved)
    statuses: list[str] = []
    server.status.connect(statuses.append)
    assert server.listen(0)
    connected = []
    client = make_client([])
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        text = "\n".join(statuses)
        assert "Listening for connections" in text
        assert "Incoming connection" in text
        assert 'Hello from "test-client"' in text
        assert "already approved" in text
        assert "Streaming screen" in text
    finally:
        client.close()
        server.close()
