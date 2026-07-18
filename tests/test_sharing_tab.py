"""Tests for the Sharing tab: the opt-in server role of the app."""

import socket

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import QMessageBox

from remotedesktop import db
from remotedesktop.config import KnownServers, Settings
from remotedesktop.inventory import ConnectionInventory
from remotedesktop.performance import PerformanceMonitor
from remotedesktop.server import SharingTab, ViewersTable

from test_discovery import free_udp_port
from test_sharing import CLIENT_ID, make_client, pump

_TEST_AUTOSTART_KEY = r"Software\remotedesktop-tests\WindowRun"


class HarnessTab(SharingTab):
    """SharingTab plus the capture attributes the tests attach."""

    messages: list[str]
    inventory: ConnectionInventory


def make_tab(credentials, tmp_path, *, discovery_port=None, connect_port=0, enabled=True):
    """A SharingTab on a temp DB with injected ports/credentials/autostart.

    `tab.messages` collects statusMessage emissions; `tab.inventory` records
    peer events the way MainWindow does.
    """
    connection = db.connect(tmp_path / "server.db")
    settings = Settings(connection)
    if enabled:
        settings.set("server_enabled", "1")
    tab = HarnessTab(
        settings=settings,
        connection=connection,
        performance=PerformanceMonitor(),
        credentials=credentials,  # always injected: never create real cert files
        discovery_port=discovery_port if discovery_port is not None else free_udp_port(),
        connect_port=connect_port,
    )
    tab.messages = []
    tab.statusMessage.connect(tab.messages.append)
    tab.inventory = ConnectionInventory()

    def apply_peer_event(e):
        # Mirrors MainWindow._record_server_peer: "revoked" deletes the row.
        if e["event"] == "revoked":
            tab.inventory.remove(e["key"])
        else:
            tab.inventory.record(
                e["key"], e["event"], name=e.get("name", ""), address=e.get("address", "")
            )

    tab.peerEvent.connect(apply_peer_event)
    tab.restore_sharing()
    return tab


def stub_approval_prompt(monkeypatch, button):
    """Answer _ask_approval's stay-on-top QMessageBox without showing any UI.

    The approval prompt is an instance QMessageBox (show + exec), not
    QMessageBox.question, so both methods are stubbed: show so nothing pops
    up on screen, exec so the "user" answers immediately.
    """
    monkeypatch.setattr(QMessageBox, "show", lambda self: None)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: button)


def test_tab_starts_not_sharing_by_default(qapp, credentials, tmp_path):
    tab = make_tab(credentials, tmp_path, enabled=False)
    try:
        assert not tab.serving
        assert tab.share_server is None and tab.responder is None
        assert not tab.share_checkbox.isChecked()
        assert "Not sharing this computer's screen" in tab._summary.text()
        assert tab.viewers_table.rowCount() == 0
    finally:
        tab.shutdown()


def test_enabling_sharing_listens_and_is_discoverable(qapp, credentials, tmp_path):
    tab = make_tab(credentials, tmp_path, enabled=False)
    try:
        tab.share_checkbox.setChecked(True)
        assert tab.serving
        assert tab._listening and tab._discoverable
        assert "Discoverable on this LAN" in tab._summary.text()
        assert "no viewers connected" in tab._summary.text()
        assert any("Discoverable as" in m for m in tab.messages)
        assert tab._settings.get("server_enabled") == "1"
    finally:
        tab.shutdown()


def test_persisted_opt_in_resumes_sharing(qapp, credentials, tmp_path):
    tab = make_tab(credentials, tmp_path, enabled=True)
    try:
        assert tab.share_checkbox.isChecked()
        assert tab.serving
    finally:
        tab.shutdown()


def test_disabling_sharing_disconnects_viewers_and_stops_discovery(
    qapp, credentials, tmp_path, monkeypatch
):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    tab = make_tab(credentials, tmp_path)
    client = make_client(tmp_path)
    names, disconnected = [], []
    client.connected.connect(names.append)
    client.disconnected.connect(lambda: disconnected.append(True))
    first_port = tab.share_server.port
    client.connect_to("127.0.0.1", first_port)
    try:
        pump(qapp, lambda: names)

        tab.share_checkbox.setChecked(False)
        pump(qapp, lambda: disconnected)
        assert not tab.serving
        assert tab.share_server is None and tab.responder is None
        assert "Not sharing this computer's screen" in tab._summary.text()
        assert tab._settings.get("server_enabled") == "0"
        assert tab.viewers_table.rowCount() == 0

        # Re-enabling shares again, and the paired client reconnects with its
        # stored token — no new approval prompt. connect_port=0 gives the
        # re-enabled server a new ephemeral port (the real app reuses the
        # fixed port), so re-key the client's stored token to the new port.
        token = tab._paired.token_for(CLIENT_ID)
        assert token is not None  # the pairing survived the toggle
        prompts = []
        tab._ask_approval = lambda cid, name: prompts.append(cid) or True
        tab.share_checkbox.setChecked(True)
        assert tab.serving
        known = KnownServers(db.connect(tmp_path / "client.db"))
        record = known.get(f"127.0.0.1:{first_port}")
        assert record is not None
        known.remember(
            f"127.0.0.1:{tab.share_server.port}", record["fingerprint"], record["token"]
        )
        reconnected = []
        client.connected.connect(reconnected.append)
        client.connect_to("127.0.0.1", tab.share_server.port)
        pump(qapp, lambda: reconnected)
        assert prompts == []  # token authenticated, nobody was asked
        assert tab._paired.token_for(CLIENT_ID) == token  # same pairing
    finally:
        client.close()
        tab.shutdown()


def test_discovery_port_conflict_is_reported(qapp, credentials, tmp_path):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("", 0))
    port = blocker.getsockname()[1]
    try:
        tab = make_tab(credentials, tmp_path, discovery_port=port, enabled=False)
        try:
            tab.share_checkbox.setChecked(True)
            assert tab._listening
            assert not tab._discoverable
            assert "Not discoverable" in tab._summary.text()
            assert any("Discovery unavailable" in m for m in tab.messages)
        finally:
            tab.shutdown()
    finally:
        blocker.close()


def test_connect_port_conflict_is_reported(qapp, credentials, tmp_path):
    # Block the port the same way the app binds it (dual-stack Any via Qt);
    # a raw IPv4-only socket would not conflict with Qt's IPv6 Any binding.
    blocker = QTcpServer()
    assert blocker.listen(QHostAddress.SpecialAddress.Any, 0)
    port = blocker.serverPort()
    try:
        tab = make_tab(credentials, tmp_path, connect_port=port, enabled=False)
        try:
            tab.share_checkbox.setChecked(True)
            assert not tab.serving
            assert tab.responder is None
            assert "Cannot share" in tab._summary.text()
        finally:
            tab.shutdown()
    finally:
        blocker.close()


def test_get_client_log_needs_sharing_and_a_client(qapp, credentials, tmp_path):
    tab = make_tab(credentials, tmp_path, enabled=False)
    try:
        tab.request_client_log()
        assert any("no connected client" in m for m in tab.messages)
    finally:
        tab.shutdown()


def test_received_client_log_opens_a_viewer_dialog(qapp, credentials, tmp_path):
    from remotedesktop.logs import PeerLogDialog

    tab = make_tab(credentials, tmp_path, enabled=False)
    try:
        tab._show_client_log("laptop", "some log text")
        dialog = tab.findChild(PeerLogDialog)
        assert dialog is not None
        assert 'Log from client "laptop"' == dialog.windowTitle()
        dialog.close()
    finally:
        tab.shutdown()


def test_approval_prompt_pairs_client_and_updates_summary(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    tab = make_tab(credentials, tmp_path)
    client = make_client(tmp_path)
    names = []
    client.connected.connect(names.append)
    client.connect_to("127.0.0.1", tab.share_server.port)
    try:
        pump(qapp, lambda: names)
        pump(qapp, lambda: "Sharing this desktop with 1 viewer(s)" in tab._summary.text())
        # The inventory tracked the pairing via peer events.
        assert tab.inventory._peers[CLIENT_ID].state == "connected (paired)"
    finally:
        client.close()
        tab.shutdown()


def test_refused_approval_denies_client(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.No)
    tab = make_tab(credentials, tmp_path)
    client = make_client(tmp_path)
    denials = []
    client.denied.connect(denials.append)
    client.connect_to("127.0.0.1", tab.share_server.port)
    try:
        pump(qapp, lambda: denials)
        assert "refused" in denials[0]
    finally:
        client.close()
        tab.shutdown()


def test_revoke_disconnects_a_connected_client(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    tab = make_tab(credentials, tmp_path)
    client = make_client(tmp_path)
    names, disconnected = [], []
    client.connected.connect(names.append)
    client.disconnected.connect(lambda: disconnected.append(True))
    client.connect_to("127.0.0.1", tab.share_server.port)
    try:
        pump(qapp, lambda: names)
        tab.revoke_client(CLIENT_ID)
        pump(qapp, lambda: disconnected)
        # Symmetric with forgetting a server: the revoked row is deleted.
        pump(qapp, lambda: CLIENT_ID not in tab.inventory._peers)
    finally:
        client.close()
        tab.shutdown()


def test_revoke_works_while_not_sharing(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    tab = make_tab(credentials, tmp_path)
    client = make_client(tmp_path)
    names = []
    client.connected.connect(names.append)
    client.connect_to("127.0.0.1", tab.share_server.port)
    try:
        pump(qapp, lambda: names)
        assert tab._paired.token_for(CLIENT_ID) is not None
        tab.share_checkbox.setChecked(False)  # stop sharing; pairing remains
        assert tab._paired.token_for(CLIENT_ID) is not None

        tab.revoke_client(CLIENT_ID)
        assert tab._paired.token_for(CLIENT_ID) is None
        assert CLIENT_ID not in tab.inventory._peers  # row deleted outright
        assert any("Revoked access" in m for m in tab.messages)
    finally:
        client.close()
        tab.shutdown()


class FakeShareServer(QObject):
    clientCountChanged = Signal(int)

    def __init__(self, viewers):
        super().__init__()
        self._viewers = viewers

    def viewers(self):
        return self._viewers


def test_viewers_table_lists_connected_client_details(qapp, credentials, tmp_path, monkeypatch):
    stub_approval_prompt(monkeypatch, QMessageBox.StandardButton.Yes)
    tab = make_tab(credentials, tmp_path)
    client = make_client(tmp_path)
    names = []
    client.connected.connect(names.append)
    assert tab.viewers_table.rowCount() == 0
    client.connect_to("127.0.0.1", tab.share_server.port)
    try:
        pump(qapp, lambda: names)
        pump(qapp, lambda: tab.viewers_table.rowCount() == 1)
        row = []
        for column in range(len(tab.viewers_table._COLUMNS)):
            item = tab.viewers_table.item(0, column)
            assert item is not None
            row.append(item.text())
        name, address, user, host, os_info, version, send, recv, rtt = row[:9]
        # The hello carried this machine's real login/host/OS/version details.
        assert name and user != "—" and host != "—"
        assert "127.0.0.1" in address and "::ffff:" not in address
        assert os_info.startswith("Windows")
        from remotedesktop import __version__

        assert version == __version__
        # No monitor tick has necessarily run yet; metrics are dashes or values.
        assert send and recv and rtt
        client.close()
        pump(qapp, lambda: tab.viewers_table.rowCount() == 0)
    finally:
        client.close()
        tab.shutdown()


def test_viewers_table_follows_the_set_share_server(qapp):
    from test_performance import FakeStream

    viewer = {
        "name": "n", "address": "a", "user": "u", "host": "h", "os": "o",
        "app_version": "1.0", "stream": FakeStream(),
    }
    table = ViewersTable(PerformanceMonitor())
    assert table.rowCount() == 0  # no server yet
    first = FakeShareServer([viewer])
    table.set_share_server(first)
    assert table.rowCount() == 1
    table.set_share_server(None)  # sharing turned off
    assert table.rowCount() == 0
    # Signals from a replaced server are ignored (disconnected).
    second = FakeShareServer([])
    table.set_share_server(second)
    first.clientCountChanged.emit(1)
    assert table.rowCount() == 0


def test_viewers_table_metric_columns_keep_constant_width(qapp):
    from remotedesktop.performance import MetricSeries
    from test_performance import FakeStream

    stream = FakeStream()
    monitor = PerformanceMonitor()
    viewer = {
        "name": "n", "address": "a", "user": "u", "host": "h", "os": "o",
        "app_version": "1.0", "stream": stream,
    }
    table = ViewersTable(monitor)
    table.set_share_server(FakeShareServer([viewer]))
    widths = [table.columnWidth(c) for c in ViewersTable._METRIC_COLUMNS]
    assert all(w > 0 for w in widths)
    # Values swinging from B/s to hundreds of MB/s must not move the columns.
    rtt_series = MetricSeries(120.0)
    rtt_series.add(2.1)
    monitor._stream_send_bps[stream] = 312.0
    monitor._stream_recv_bps[stream] = 5.0
    monitor._stream_rtt[stream] = rtt_series
    table.refresh()
    assert [table.columnWidth(c) for c in ViewersTable._METRIC_COLUMNS] == widths
    monitor._stream_send_bps[stream] = 250.0 * 1024 * 1024
    rtt_series.add(1234.5)
    table.refresh()
    assert [table.columnWidth(c) for c in ViewersTable._METRIC_COLUMNS] == widths
    send_item = table.item(0, ViewersTable._RATE_COLUMNS[0])
    assert send_item is not None and send_item.text() == "250.0 MB/s"


def test_viewers_table_flags_a_major_version_mismatch(qapp):
    from remotedesktop import __version__
    from test_performance import FakeStream

    def viewer(version, stream):
        return {
            "name": "n", "address": "a", "user": "u", "host": "h", "os": "o",
            "app_version": version, "stream": stream,
        }

    table = ViewersTable(PerformanceMonitor())
    table.set_share_server(
        FakeShareServer(
            [viewer(__version__, FakeStream()), viewer("99.0.0", FakeStream()), viewer("", FakeStream())]
        )
    )
    column = ViewersTable._COLUMNS.index("Version")
    cells = []
    for row in range(3):
        item = table.item(row, column)
        assert item is not None
        cells.append(item.text())
    assert cells == [__version__, "99.0.0 ⚠", "—"]
