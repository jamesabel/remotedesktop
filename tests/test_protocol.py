import struct

from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from remotedesktop.protocol import MessageStream

from test_sharing import pump


def tcp_pair(qapp):
    """A connected loopback TCP pair: (listener, server-side socket, client socket).

    The listener must stay referenced for the server-side socket to live.
    """
    listener = QTcpServer()
    assert listener.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    client = QTcpSocket()
    client.connectToHost("127.0.0.1", listener.serverPort())
    pump(qapp, lambda: listener.hasPendingConnections())
    server_sock = listener.nextPendingConnection()
    pump(qapp, lambda: client.state() == QTcpSocket.SocketState.ConnectedState)
    return listener, server_sock, client


def test_json_and_frame_roundtrip(qapp):
    listener, server_sock, client_sock = tcp_pair(qapp)
    sender = MessageStream(client_sock)
    receiver = MessageStream(server_sock)
    messages, frames = [], []
    receiver.jsonReceived.connect(messages.append)
    receiver.frameReceived.connect(frames.append)
    sender.send_json({"type": "hello", "n": 1})
    sender.send_frame(b"\xff\xd8jpeg-bytes")
    pump(qapp, lambda: messages and frames)
    assert messages == [{"type": "hello", "n": 1}]
    assert frames == [b"\xff\xd8jpeg-bytes"]


def test_message_split_across_reads(qapp):
    listener, server_sock, client_sock = tcp_pair(qapp)
    receiver = MessageStream(server_sock)
    messages = []
    receiver.jsonReceived.connect(messages.append)
    payload = b'{"type": "hello"}'
    raw = struct.pack(">IB", len(payload), 0) + payload
    client_sock.write(raw[:3])  # not even a whole header
    client_sock.flush()
    pump(qapp, lambda: server_sock.bytesAvailable() >= 3)
    assert messages == []
    client_sock.write(raw[3:])
    client_sock.flush()
    pump(qapp, lambda: messages)
    assert messages == [{"type": "hello"}]


def test_oversized_payload_aborts_the_socket(qapp):
    listener, server_sock, client_sock = tcp_pair(qapp)
    receiver = MessageStream(server_sock, max_payload=1024)  # noqa: F841 (must stay alive)
    client_sock.write(struct.pack(">IB", 2048, 0))
    client_sock.flush()
    pump(qapp, lambda: server_sock.state() != QTcpSocket.SocketState.ConnectedState)


def test_malformed_json_aborts_the_socket(qapp):
    listener, server_sock, client_sock = tcp_pair(qapp)
    receiver = MessageStream(server_sock)  # noqa: F841 (must stay alive)
    payload = b"this is not json"
    client_sock.write(struct.pack(">IB", len(payload), 0) + payload)
    client_sock.flush()
    pump(qapp, lambda: server_sock.state() != QTcpSocket.SocketState.ConnectedState)


def test_unknown_kind_is_skipped(qapp):
    listener, server_sock, client_sock = tcp_pair(qapp)
    sender = MessageStream(client_sock)
    receiver = MessageStream(server_sock)
    messages = []
    receiver.jsonReceived.connect(messages.append)
    client_sock.write(struct.pack(">IB", 2, 9) + b"xx")  # future message kind
    sender.send_json({"still": "works"})
    pump(qapp, lambda: messages)
    assert messages == [{"still": "works"}]
    assert server_sock.state() == QTcpSocket.SocketState.ConnectedState
