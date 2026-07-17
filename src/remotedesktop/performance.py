"""Connection performance monitoring: bandwidth and round-trip time.

`PerformanceMonitor` is an opt-in collaborator for `ShareServer`/`ShareClient`
(the `clipboard=` pattern): while at least one admitted stream is attached, a
1 s timer samples the streams' framed byte counters into rolling bandwidth
series and pings the peer. Each side measures its own round-trip time and
piggybacks the latest measurement on its next ping (`"rtt"`), so both ends can
graph both directions. Peers that predate this feature simply never answer
pings — the RTT series stay empty and nothing else is affected.

The same machinery doubles as dead-connection detection: once the active
stream's peer has proven it answers pings, a peer that then goes completely
silent (no bytes received at all) for `dead_after_seconds` is presumed gone
and `connectionLost` is emitted, once. A half-open TCP connection is otherwise
invisible here — an unchanged screen legitimately sends nothing, so a frozen
last frame looks exactly like a live static desktop. Peers that never answered
a ping (pre-feature versions) never arm the detector, so they are never
falsely reported.

Wire messages (JSON, admitted streams only):
    {"type": "ping", "id": <int>, "rtt": <ms, omitted before first measurement>}
    {"type": "pong", "id": <int>}   # pure echo

`PerformanceTab` shows the graphs. It schedules no paint work while hidden
(background pages of a QTabWidget are hidden widgets): sampling continues in
the monitor, and the tab repaints on show with the accrued history.

All timing uses time.monotonic(), injectable as `clock=` for tests.
"""

import itertools
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, QPointF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPolygonF, QShowEvent
from PySide6.QtWidgets import QVBoxLayout, QWidget

_log = logging.getLogger("remotedesktop.performance")


class StreamLike(Protocol):
    """What the monitor needs from a stream: `protocol.MessageStream`
    satisfies this, and tests substitute a plain fake with a recording
    send_json."""

    bytes_sent: int
    bytes_received: int

    def send_json(self, message: dict) -> None: ...


DEFAULT_WINDOW_SECONDS = 120.0
DEFAULT_INTERVAL_MS = 1000
DEFAULT_DEAD_AFTER_SECONDS = 10.0
_PING_PRUNE_SECONDS = 30.0
_HEARTBEAT_TICKS = 10  # one debug-log line per this many samples


class MetricSeries:
    """(monotonic time, value) samples kept for a rolling time window."""

    def __init__(self, window_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window_seconds
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, value: float) -> None:
        self._samples.append((self._clock(), value))
        self._trim()

    def samples(self) -> list[tuple[float, float]]:
        self._trim()
        return list(self._samples)

    def latest(self) -> float | None:
        return self._samples[-1][1] if self._samples else None

    def clear(self) -> None:
        self._samples.clear()

    def set_window(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._trim()

    def _trim(self) -> None:
        cutoff = self._clock() - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()


class PerformanceMonitor(QObject):
    """Samples bandwidth and measures RTT for the attached streams.

    The timer runs only while streams are attached (the ShareServer._timer
    discipline), so an idle app schedules no periodic work. Bandwidth
    aggregates all attached streams; pings go to the most recently attached
    stream only, which keeps the RTT series a single coherent line when a
    server has several viewers.
    """

    updated = Signal()  # one emit per sample tick
    connectionLost = Signal(object)  # the active stream whose responsive peer went silent

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        dead_after_seconds: float = DEFAULT_DEAD_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._window = window_seconds
        self._dead_after = dead_after_seconds
        self.send_bps = MetricSeries(window_seconds, clock=clock)
        self.recv_bps = MetricSeries(window_seconds, clock=clock)
        self.rtt_ms = MetricSeries(window_seconds, clock=clock)
        self.peer_rtt_ms = MetricSeries(window_seconds, clock=clock)
        self._streams: list[StreamLike] = []
        self._baselines: dict[StreamLike, tuple[int, int]] = {}
        self._active: StreamLike | None = None
        # Dead-connection detection, all scoped to the active stream: when its
        # bytes_received counter last grew, whether its peer ever answered a
        # ping (arms the detector), and whether a loss was already reported.
        self._last_data: float | None = None
        self._peer_responsive = False
        self._lost_reported = False
        self._pending: dict[int, float] = {}
        self._ids = itertools.count(1)
        self._last_tick: float | None = None
        self._tick_count = 0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_tick)

    @property
    def window_seconds(self) -> float:
        return self._window

    @property
    def dead_after_seconds(self) -> float:
        return self._dead_after

    def set_window_seconds(self, window_seconds: float) -> None:
        self._window = window_seconds
        for series in (self.send_bps, self.recv_bps, self.rtt_ms, self.peer_rtt_ms):
            series.set_window(window_seconds)

    def add_stream(self, stream: StreamLike) -> None:
        if stream not in self._baselines:
            self._streams.append(stream)
        self._baselines[stream] = (stream.bytes_sent, stream.bytes_received)
        self._activate(stream)
        if not self._timer.isActive():
            self._last_tick = None
            self._timer.start()

    def remove_stream(self, stream: StreamLike) -> None:
        if stream not in self._baselines:
            return
        self._streams.remove(stream)
        del self._baselines[stream]
        if self._active is stream:
            self._activate(self._streams[-1] if self._streams else None)
        if not self._streams:
            self._timer.stop()
            self._pending.clear()
            self._last_tick = None

    def _activate(self, stream: StreamLike | None) -> None:
        self._active = stream
        self._last_data = self._clock() if stream is not None else None
        self._peer_responsive = False
        self._lost_reported = False

    def reset(self) -> None:
        """Detach everything and clear the graphs (new connection attempt)."""
        for stream in list(self._streams):
            self.remove_stream(stream)
        for series in (self.send_bps, self.recv_bps, self.rtt_ms, self.peer_rtt_ms):
            series.clear()

    def handle_message(self, stream: StreamLike, message: dict) -> None:
        match message.get("type"):
            case "ping":
                stream.send_json({"type": "pong", "id": message.get("id")})
                rtt = message.get("rtt")
                if stream is self._active:
                    # A peer that pings us runs this feature too — that arms
                    # the silence detector just as well as a pong does.
                    self._peer_responsive = True
                    if isinstance(rtt, (int, float)):
                        self.peer_rtt_ms.add(float(rtt))
            case "pong":
                if stream is self._active:
                    self._peer_responsive = True
                ping_id = message.get("id")
                if isinstance(ping_id, int):
                    sent = self._pending.pop(ping_id, None)
                    if sent is not None:
                        self.rtt_ms.add((self._clock() - sent) * 1000.0)

    def _on_tick(self) -> None:
        now = self._clock()
        delta_sent = delta_received = 0
        for stream in self._streams:
            base_sent, base_received = self._baselines[stream]
            delta_sent += stream.bytes_sent - base_sent
            delta_received += stream.bytes_received - base_received
            if stream is self._active and stream.bytes_received > base_received:
                self._last_data = now
            self._baselines[stream] = (stream.bytes_sent, stream.bytes_received)
        if self._last_tick is not None and (elapsed := now - self._last_tick) > 0:
            self.send_bps.add(delta_sent / elapsed)
            self.recv_bps.add(delta_received / elapsed)
        self._last_tick = now

        if self._active is not None:
            for ping_id in [i for i, t in self._pending.items() if now - t > _PING_PRUNE_SECONDS]:
                del self._pending[ping_id]
            ping_id = next(self._ids)
            self._pending[ping_id] = now
            ping: dict = {"type": "ping", "id": ping_id}
            if (rtt := self.rtt_ms.latest()) is not None:
                ping["rtt"] = rtt
            self._active.send_json(ping)

            if (
                self._peer_responsive
                and not self._lost_reported
                and self._last_data is not None
                and now - self._last_data > self._dead_after
            ):
                self._lost_reported = True
                _log.warning(
                    "No data from the peer for %.0f s (pings unanswered) — "
                    "connection presumed lost",
                    now - self._last_data,
                )
                self.connectionLost.emit(self._active)

        self._tick_count += 1
        if self._tick_count % _HEARTBEAT_TICKS == 0:
            _log.debug(
                "send %.1f KB/s, recv %.1f KB/s, rtt %s, peer rtt %s, %d stream(s)",
                (self.send_bps.latest() or 0.0) / 1024,
                (self.recv_bps.latest() or 0.0) / 1024,
                f"{rtt:.1f} ms" if (rtt := self.rtt_ms.latest()) is not None else "n/a",
                f"{peer:.1f} ms" if (peer := self.peer_rtt_ms.latest()) is not None else "n/a",
                len(self._streams),
            )
        self.updated.emit()


def format_rate(bytes_per_second: float) -> str:
    if bytes_per_second >= 1024 * 1024:
        return f"{bytes_per_second / (1024 * 1024):.1f} MB/s"
    if bytes_per_second >= 1024:
        return f"{bytes_per_second / 1024:.1f} KB/s"
    return f"{bytes_per_second:.0f} B/s"


def format_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


class GraphWidget(QWidget):
    """A rolling line graph over the monitor's window, painted with QPainter.

    Holds references to the monitor's live MetricSeries and pulls their data
    inside paintEvent, so a hidden graph does no per-sample work at all.
    """

    def __init__(
        self,
        title: str,
        series: list[tuple[str, QColor, MetricSeries]],
        monitor: PerformanceMonitor,
        format_value: Callable[[float], str],
        *,
        clock: Callable[[], float] = time.monotonic,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._series = series
        self._monitor = monitor
        self._format_value = format_value
        self._clock = clock
        self.setMinimumSize(300, 120)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.fillRect(self.rect(), palette.base())
        painter.setPen(palette.color(palette.ColorRole.Mid))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setPen(palette.color(palette.ColorRole.Text))
        painter.drawText(8, 16, self._title)

        window = self._monitor.window_seconds
        now = self._clock()
        data = [(label, color, series.samples()) for label, color, series in self._series]
        if not any(samples for _label, _color, samples in data):
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "no data"
            )
            return
        y_max = max(
            (value for _l, _c, samples in data for _t, value in samples), default=1.0
        )
        y_max = max(y_max, 1.0)

        graph = self.rect().adjusted(8, 24, -8, -20)
        for label, color, samples in data:
            if not samples:
                continue
            polyline = QPolygonF()
            for t, value in samples:
                x = graph.left() + graph.width() * (1 - (now - t) / window)
                y = graph.bottom() - graph.height() * (value / y_max)
                polyline.append(QPointF(x, y))
            painter.setPen(color)
            painter.drawPolyline(polyline)

        # Legend with latest values along the bottom edge.
        x = graph.left()
        for label, color, samples in data:
            latest = samples[-1][1] if samples else None
            text = f"{label} {self._format_value(latest)}" if latest is not None else f"{label} —"
            painter.setPen(color)
            painter.drawText(x, self.rect().bottom() - 6, text)
            x += painter.fontMetrics().horizontalAdvance(text) + 16
        painter.setPen(palette.color(palette.ColorRole.Text))
        painter.drawText(
            self.rect().right() - 8 - painter.fontMetrics().horizontalAdvance(self._format_value(y_max)),
            16,
            self._format_value(y_max),
        )


class PerformanceTab(QWidget):
    """Bandwidth and round-trip-time graphs for the current connection.

    Graphs repaint only while this tab is visible: sampling continues in the
    monitor regardless, but a background tab schedules zero paint work.
    """

    def __init__(self, monitor: PerformanceMonitor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._monitor = monitor
        self.bandwidth_graph = GraphWidget(
            "Bandwidth",
            [
                ("send", QColor("#2196f3"), monitor.send_bps),
                ("receive", QColor("#4caf50"), monitor.recv_bps),
            ],
            monitor,
            format_rate,
        )
        self.ping_graph = GraphWidget(
            "Round-trip time",
            [
                ("this side", QColor("#ff9800"), monitor.rtt_ms),
                ("peer", QColor("#9c27b0"), monitor.peer_rtt_ms),
            ],
            monitor,
            format_ms,
        )
        layout = QVBoxLayout(self)
        layout.addWidget(self.bandwidth_graph, stretch=1)
        layout.addWidget(self.ping_graph, stretch=1)
        monitor.updated.connect(self._refresh)

    def _refresh(self) -> None:
        if not self.isVisible():
            return  # hidden tab page: schedule no paint work at all
        self._refresh_now()

    def _refresh_now(self) -> None:
        self.bandwidth_graph.update()
        self.ping_graph.update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh_now()  # repaint with the history accrued while hidden
