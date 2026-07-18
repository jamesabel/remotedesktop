import json

import pytest
from PySide6.QtGui import QShowEvent
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket
from PySide6.QtWidgets import QApplication

from remotedesktop import db
from remotedesktop.config import KnownServers, PairedClients
from remotedesktop.performance import (
    GraphWidget,
    MetricSeries,
    PerformanceMonitor,
    PerformanceTab,
    nice_ceiling,
    rate_unit,
)
from remotedesktop.protocol import MessageStream
from remotedesktop.sharing import ShareClient, ShareServer

from test_sharing import IDENTITY, pump


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeStream:
    def __init__(self) -> None:
        self.bytes_sent = 0
        self.bytes_received = 0
        self.sent: list[dict] = []

    def send_json(self, message: dict) -> None:
        self.sent.append(message)


def test_metric_series_trims_to_window():
    clock = FakeClock()
    series = MetricSeries(10.0, clock=clock)
    series.add(1.0)
    clock.advance(5)
    series.add(2.0)
    clock.advance(6)  # first sample is now 11 s old, second 6 s
    assert [value for _t, value in series.samples()] == [2.0]
    series.set_window(1.0)  # tightening the window re-trims immediately
    assert series.samples() == []


def test_sample_statistics_summarize_the_window():
    from remotedesktop.performance import sample_statistics

    assert sample_statistics([]) is None
    single = sample_statistics([5.0])
    assert single == {
        "count": 1, "mean": 5.0, "min": 5.0, "max": 5.0, "p99": 5.0, "jitter": 0.0,
    }
    stats = sample_statistics([2.0, 4.0, 2.0, 12.0])
    assert stats is not None
    assert stats["count"] == 4
    assert stats["mean"] == 5.0
    assert stats["min"] == 2.0 and stats["max"] == 12.0
    assert stats["p99"] == 12.0  # nearest-rank on a small sample = max
    # Jitter: mean |consecutive delta| = (2 + 2 + 10) / 3
    assert stats["jitter"] == pytest.approx(14.0 / 3.0)
    # A steady series has zero jitter no matter its level.
    steady = sample_statistics([100.0] * 50)
    assert steady is not None and steady["jitter"] == 0.0
    # p99 excludes a single outlier once there are >= 100 samples.
    many = sample_statistics([10.0] * 99 + [500.0])
    assert many is not None and many["p99"] == 10.0


def test_metric_series_statistics_follow_the_window():
    clock = FakeClock()
    series = MetricSeries(10.0, clock=clock)
    assert series.statistics() is None
    series.add(100.0)  # will age out of the window
    clock.advance(8.0)
    for value in (2.0, 4.0):
        series.add(value)
    clock.advance(3.0)  # first sample is now 11 s old
    stats = series.statistics()
    assert stats is not None
    assert stats["count"] == 2 and stats["max"] == 4.0  # 100.0 aged out


def test_monitor_samples_aggregate_bandwidth(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(window_seconds=60, clock=clock)
    first, second = FakeStream(), FakeStream()
    monitor.add_stream(first)
    monitor._on_tick()  # first tick establishes the baseline; no rate sample
    assert monitor.send_bps.latest() is None
    first.bytes_sent += 1000
    first.bytes_received += 500
    clock.advance(2.0)
    monitor._on_tick()
    assert monitor.send_bps.latest() == 500.0
    assert monitor.recv_bps.latest() == 250.0
    # A second stream aggregates; removing it never yields a negative rate.
    monitor.add_stream(second)
    second.bytes_sent += 300
    clock.advance(1.0)
    monitor._on_tick()
    assert monitor.send_bps.latest() == 300.0
    monitor.remove_stream(second)
    clock.advance(1.0)
    monitor._on_tick()
    assert monitor.send_bps.latest() == 0.0


def test_monitor_skips_rate_on_zero_elapsed(qapp):
    monitor = PerformanceMonitor(clock=FakeClock())  # clock never advances
    monitor.add_stream(FakeStream())
    monitor._on_tick()
    monitor._on_tick()
    assert monitor.send_bps.latest() is None


def test_ping_pong_records_rtt_and_replies(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(clock=clock)
    stream = FakeStream()
    monitor.add_stream(stream)
    monitor._on_tick()
    ping = stream.sent[-1]
    assert ping["type"] == "ping"
    assert "rtt" not in ping  # nothing measured yet
    clock.advance(0.05)
    monitor.handle_message(stream, {"type": "pong", "id": ping["id"]})
    assert monitor.rtt_ms.latest() == pytest.approx(50.0)
    clock.advance(1.0)
    monitor._on_tick()
    assert stream.sent[-1]["rtt"] == pytest.approx(50.0)  # piggybacked
    # An incoming ping is answered with an echoing pong and records peer RTT.
    monitor.handle_message(stream, {"type": "ping", "id": 99, "rtt": 12.5})
    assert stream.sent[-1] == {"type": "pong", "id": 99}
    assert monitor.peer_rtt_ms.latest() == 12.5


def test_per_stream_metrics_track_each_viewer(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(clock=clock)
    first, second = FakeStream(), FakeStream()
    monitor.add_stream(first)
    monitor.add_stream(second)
    monitor._on_tick()  # baselines; every attached stream gets its own ping
    assert [m["type"] for m in first.sent] == ["ping"]
    assert [m["type"] for m in second.sent] == ["ping"]
    first.bytes_sent += 1000
    second.bytes_sent += 500
    clock.advance(2.0)
    monitor._on_tick()
    assert monitor.metrics_for(first)["send_bps"] == 500.0
    assert monitor.metrics_for(second)["send_bps"] == 250.0
    # Each stream's pong records that stream's RTT; the graph series only
    # follows the active (most recently attached) stream.
    clock.advance(0.05)
    monitor.handle_message(first, {"type": "pong", "id": first.sent[-1]["id"]})
    assert monitor.metrics_for(first)["rtt_ms"] == pytest.approx(50.0)
    assert monitor.rtt_ms.latest() is None  # first is not the active stream
    # An incoming ping's piggybacked measurement lands on its own stream.
    monitor.handle_message(first, {"type": "ping", "id": 99, "rtt": 12.5})
    assert monitor.metrics_for(first)["peer_rtt_ms"] == 12.5
    assert monitor.peer_rtt_ms.latest() is None
    # A pong answered on the wrong stream is ignored.
    monitor.handle_message(first, {"type": "pong", "id": second.sent[-1]["id"]})
    assert monitor.metrics_for(second)["rtt_ms"] is None
    # Removing a stream clears its numbers.
    monitor.remove_stream(first)
    assert monitor.metrics_for(first) == {
        "send_bps": None, "recv_bps": None, "rtt_ms": None, "peer_rtt_ms": None,
        "rtt_stats": None,
    }


def test_per_stream_rtt_statistics_cover_the_window(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(clock=clock)
    stream = FakeStream()
    monitor.add_stream(stream)
    for rtt_s in (0.010, 0.020, 0.090):
        monitor._on_tick()
        clock.advance(rtt_s)
        monitor.handle_message(stream, {"type": "pong", "id": stream.sent[-1]["id"]})
        clock.advance(1.0)
    stats = monitor.metrics_for(stream)["rtt_stats"]
    assert stats is not None
    assert stats["count"] == 3
    assert stats["min"] == pytest.approx(10.0)
    assert stats["max"] == pytest.approx(90.0)
    assert stats["mean"] == pytest.approx(40.0)
    assert monitor.metrics_for(stream)["rtt_ms"] == pytest.approx(90.0)


def test_stale_or_malformed_pongs_are_ignored(qapp):
    monitor = PerformanceMonitor(clock=FakeClock())
    stream = FakeStream()
    monitor.add_stream(stream)
    monitor.handle_message(stream, {"type": "pong", "id": 12345})
    monitor.handle_message(stream, {"type": "pong", "id": "weird"})
    monitor.handle_message(stream, {"type": "pong"})
    assert monitor.rtt_ms.latest() is None


def test_reset_clears_everything_and_stops_timer(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(clock=clock)
    monitor.add_stream(FakeStream())
    monitor._on_tick()
    clock.advance(1.0)
    monitor._on_tick()
    assert monitor._timer.isActive()
    monitor.reset()
    assert not monitor._timer.isActive()
    assert monitor.send_bps.samples() == []
    assert monitor.rtt_ms.samples() == []
    assert monitor._pending == {}


def test_message_stream_counts_framed_bytes(qapp):
    listener = QTcpServer()
    assert listener.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    out_sock = QTcpSocket()
    out_sock.connectToHost("127.0.0.1", listener.serverPort())
    pump(qapp, lambda: listener.hasPendingConnections())
    in_sock = listener.nextPendingConnection()
    sender, receiver = MessageStream(out_sock), MessageStream(in_sock)
    got = []
    receiver.jsonReceived.connect(got.append)
    receiver.frameReceived.connect(got.append)
    try:
        sender.send_json({"a": 1})
        sender.send_frame(b"xyz")
        pump(qapp, lambda: len(got) == 2)
        # 4-byte length + 1 kind byte per message, plus the payloads.
        expected = (5 + len(json.dumps({"a": 1}).encode())) + (5 + 3)
        assert sender.bytes_sent == expected
        assert receiver.bytes_received == expected
    finally:
        out_sock.abort()
        listener.close()


def make_pair(credentials, tmp_path, *, server_perf, client_perf):
    server = ShareServer(
        approve_client=lambda *_: True,
        credentials=credentials,
        paired=PairedClients(db.connect(tmp_path / "server.db")),
        performance=server_perf,
    )
    assert server.listen(0)
    client = ShareClient(
        identity=IDENTITY,
        known_servers=KnownServers(db.connect(tmp_path / "client.db")),
        performance=client_perf,
    )
    return server, client


def test_server_viewers_snapshot_carries_hello_details(qapp, credentials, tmp_path):
    server_perf = PerformanceMonitor(interval_ms=50)
    server, client = make_pair(
        credentials,
        tmp_path,
        server_perf=server_perf,
        client_perf=PerformanceMonitor(interval_ms=50),  # answers the pings
    )
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        viewers = server.viewers()
        assert len(viewers) == 1
        viewer = viewers[0]
        assert viewer["name"] and viewer["user"] and viewer["host"]
        assert viewer["os"].startswith("Windows")
        assert "127.0.0.1" in viewer["address"]
        # The stream key links the snapshot to per-viewer metrics.
        pump(qapp, lambda: server_perf.metrics_for(viewer["stream"])["rtt_ms"] is not None)
    finally:
        client.close()
        server.close()


def test_rtt_and_bandwidth_measured_on_both_ends(qapp, credentials, tmp_path):
    server_perf = PerformanceMonitor(interval_ms=50)
    client_perf = PerformanceMonitor(interval_ms=50)
    server, client = make_pair(
        credentials, tmp_path, server_perf=server_perf, client_perf=client_perf
    )
    client.connect_to("127.0.0.1", server.port)
    try:
        # Both sides measure their own RTT ...
        pump(qapp, lambda: client_perf.rtt_ms.latest() is not None)
        pump(qapp, lambda: server_perf.rtt_ms.latest() is not None)
        # ... and learn the peer's measurement from piggybacked pings.
        pump(qapp, lambda: client_perf.peer_rtt_ms.latest() is not None)
        pump(qapp, lambda: server_perf.peer_rtt_ms.latest() is not None)
        for series in (client_perf.rtt_ms, client_perf.peer_rtt_ms):
            assert all(value >= 0 for _t, value in series.samples())
        # Bandwidth flows: frames server->client, pings both ways.
        pump(qapp, lambda: client_perf.recv_bps.latest() is not None)
        pump(qapp, lambda: server_perf.send_bps.latest() is not None)
        assert any(value > 0 for _t, value in client_perf.recv_bps.samples())
    finally:
        client.close()
        server.close()


def test_old_peer_leaves_rtt_series_empty(qapp, credentials, tmp_path):
    # A performance-less server takes the exact code path a 0.7.0 server
    # takes for ping messages: silently dropped, never answered.
    client_perf = PerformanceMonitor(interval_ms=50)
    server, client = make_pair(
        credentials, tmp_path, server_perf=None, client_perf=client_perf
    )
    frames = []
    client.frameReceived.connect(frames.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: len(client_perf.recv_bps.samples()) >= 2)
        assert client_perf.rtt_ms.latest() is None
        assert client_perf.peer_rtt_ms.latest() is None
        assert frames  # unanswered pings don't harm the connection
    finally:
        client.close()
        server.close()


def arm(monitor: PerformanceMonitor, stream: FakeStream, clock: FakeClock) -> None:
    """Answer a ping (proving the peer responsive) and tick again so the
    pong's bytes are observed — the silence deadline starts fresh from here."""
    monitor._on_tick()
    stream.bytes_received += 10  # the pong's bytes arriving
    monitor.handle_message(stream, {"type": "pong", "id": stream.sent[-1]["id"]})
    clock.advance(1.0)
    monitor._on_tick()


def test_connection_lost_after_responsive_peer_goes_silent(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(dead_after_seconds=10.0, clock=clock)
    stream = FakeStream()
    lost = []
    monitor.connectionLost.connect(lost.append)
    monitor.add_stream(stream)
    arm(monitor, stream, clock)
    clock.advance(9.0)
    monitor._on_tick()  # bytes arrived 9 s ago: within the deadline
    assert lost == []
    clock.advance(2.0)
    monitor._on_tick()  # 11 s of silence
    assert lost == [stream]
    clock.advance(5.0)
    monitor._on_tick()  # reported once, not on every subsequent tick
    assert lost == [stream]


def test_incoming_data_defers_connection_lost(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(dead_after_seconds=10.0, clock=clock)
    stream = FakeStream()
    lost = []
    monitor.connectionLost.connect(lost.append)
    monitor.add_stream(stream)
    arm(monitor, stream, clock)
    for _ in range(4):  # 24 s total, but data keeps arriving every 6 s
        clock.advance(6.0)
        stream.bytes_received += 1
        monitor._on_tick()
    assert lost == []


def test_incoming_ping_also_arms_the_detector(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(dead_after_seconds=10.0, clock=clock)
    stream = FakeStream()
    lost = []
    monitor.connectionLost.connect(lost.append)
    monitor.add_stream(stream)
    monitor.handle_message(stream, {"type": "ping", "id": 1})
    clock.advance(11.0)
    monitor._on_tick()
    assert lost == [stream]


def test_silent_legacy_peer_is_never_reported_lost(qapp):
    # A peer that never answered a ping (pre-0.9 server) must never trip the
    # detector: its silence on a static screen is indistinguishable from life.
    clock = FakeClock()
    monitor = PerformanceMonitor(dead_after_seconds=10.0, clock=clock)
    stream = FakeStream()
    lost = []
    monitor.connectionLost.connect(lost.append)
    monitor.add_stream(stream)
    for _ in range(10):
        clock.advance(60.0)
        monitor._on_tick()
    assert lost == []


def test_reattaching_a_stream_resets_the_detector(qapp):
    clock = FakeClock()
    monitor = PerformanceMonitor(dead_after_seconds=10.0, clock=clock)
    stream = FakeStream()
    lost = []
    monitor.connectionLost.connect(lost.append)
    monitor.add_stream(stream)
    arm(monitor, stream, clock)
    clock.advance(11.0)
    monitor._on_tick()
    assert lost == [stream]
    # A reconnect detaches and reattaches: the new session starts unarmed
    # with a fresh deadline, so the stale silence is not reported again.
    monitor.remove_stream(stream)
    monitor.add_stream(stream)
    clock.advance(60.0)
    monitor._on_tick()
    assert lost == [stream]


def test_share_client_drops_the_connection_on_lost_signal(qapp, tmp_path):
    monitor = PerformanceMonitor()
    client = ShareClient(
        identity=IDENTITY,
        known_servers=KnownServers(db.connect(tmp_path / "client.db")),
        performance=monitor,
    )
    statuses, disconnects = [], []
    client.status.connect(statuses.append)
    client.disconnected.connect(lambda: disconnects.append(True))
    monitor.connectionLost.emit(FakeStream())  # someone else's stream: ignored
    assert disconnects == []
    monitor.connectionLost.emit(client._stream)
    assert disconnects == [True]
    assert any("Connection lost" in s and "10 s" in s for s in statuses)


def test_nice_ceiling_rounds_up_to_1_2_5():
    assert nice_ceiling(0.0) == 1.0
    assert nice_ceiling(-3.0) == 1.0
    assert nice_ceiling(0.7) == 1.0
    assert nice_ceiling(1.0) == 1.0
    assert nice_ceiling(1.5) == 2.0
    assert nice_ceiling(3.0) == 5.0
    assert nice_ceiling(42.0) == 50.0
    assert nice_ceiling(50.0) == 50.0
    assert nice_ceiling(99.0) == 100.0
    assert nice_ceiling(2049.0) == 5000.0
    assert nice_ceiling(0.03) == pytest.approx(0.05)


def test_rate_unit_matches_format_rate_unit():
    assert rate_unit(500.0) == 1.0
    assert rate_unit(2048.0) == 1024.0
    assert rate_unit(3 * 1024 * 1024) == 1024.0 * 1024.0
    # A round tick count in the displayed unit: 70000 B/s displays in KB/s,
    # and the resulting ceiling is 100 KB/s exactly.
    unit = rate_unit(70000.0)
    assert unit * nice_ceiling(70000.0 / unit) == 100.0 * 1024.0


def test_rtt_lines_are_named_for_the_side_that_pings(qapp):
    monitor = PerformanceMonitor()
    client_tab = PerformanceTab(monitor, local="client", remote="server")
    server_tab = PerformanceTab(monitor, local="server", remote="client")
    # This side's own measurement (rtt_ms) is listed first; the same physical
    # measurement carries the same name in both apps, and each label spells
    # out the full loop so it can't be read as a one-way leg.
    assert [label for label, _c, series in client_tab.ping_graph._series] == [
        "client → server → client",
        "server → client → server",
    ]
    assert [label for label, _c, series in server_tab.ping_graph._series] == [
        "server → client → server",
        "client → server → client",
    ]


def test_graph_widgets_render_headless(qapp):
    seeded = PerformanceMonitor()
    seeded.send_bps.add(100.0)
    seeded.send_bps.add(2048.0)
    seeded.recv_bps.add(50.0)
    seeded.rtt_ms.add(12.5)  # exercises the RTT graph's grid/axes path too
    for monitor in (seeded, PerformanceMonitor()):  # data and "no data" paths
        tab = PerformanceTab(monitor)
        tab.resize(400, 300)
        pixmap = tab.grab()
        assert not pixmap.isNull()
        assert pixmap.width() > 0


def test_tab_schedules_no_paints_while_hidden(qapp, monkeypatch):
    monitor = PerformanceMonitor()
    _tab = PerformanceTab(monitor)  # subscribes to monitor.updated
    painted = []
    monkeypatch.setattr(GraphWidget, "update", lambda self: painted.append(self))
    monitor.updated.emit()  # tab was never shown -> not visible
    assert painted == []
    monkeypatch.setattr(PerformanceTab, "isVisible", lambda self: True)
    monitor.updated.emit()
    assert len(painted) == 2  # both graphs refreshed


def test_tab_refreshes_on_show_event(qapp, monkeypatch):
    monitor = PerformanceMonitor()
    tab = PerformanceTab(monitor)
    painted = []
    monkeypatch.setattr(GraphWidget, "update", lambda self: painted.append(self))
    QApplication.sendEvent(tab, QShowEvent())
    assert len(painted) == 2
