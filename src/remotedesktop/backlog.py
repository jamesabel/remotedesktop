"""Per-viewer send-backlog governor.

Unsent bytes sitting in a viewer's socket are pure latency: the viewer must
receive and render all of them before it shows anything current. So the
server withholds frames from a viewer whose queue is longer than a cap —
and the right cap depends on the link. 2 MiB is ~200 ms of queue on 100
Mbit/s but ~17 ms on gigabit, so a fixed number is either laggy on the slow
link or needlessly strict on the fast one.

`BacklogGovernor` sizes the cap from what it observes on the stream:

- **Bandwidth**: the socket's drain rate, sampled over intervals in which
  the queue never emptied (so the sample reflects the link, not idleness).
  A fast-up / slow-down average, starting from 100 Mbit/s — the slowest LAN
  the app is designed for — so a gigabit link is recognized within a few
  busy ticks. The cap is that throughput times `QUEUE_TARGET_SECONDS`.
- **Latency**: the in-band round-trip time (from the performance monitor's
  pings, which queue behind frame data like everything else) against its
  running floor. Excess above the target means bytes the byte counter can't
  see are queued somewhere (the kernel's send buffer, the network), so the
  cap is halved; it recovers geometrically while the excess stays small.
  The floor drifts upward slowly so a genuinely changed baseline is
  re-learned within about a minute.

Everything is pure arithmetic with an injectable clock — no Qt, no I/O.
"""

import time
from collections.abc import Callable

# How much queued frame data a viewer may have before frames are withheld
# from it, expressed as time on the wire. 50 ms keeps the added latency
# below what a person notices while still letting one keyframe drain.
QUEUE_TARGET_SECONDS = 0.05
# The link assumed until measured: 100 Mbit/s, the slowest LAN the app
# targets (readme: "a modern LAN, often wired — at least 100 Mbit/s").
ASSUMED_BYTES_PER_SECOND = 100_000_000 / 8
# Bounds on the cap whatever the estimates say: never so small that a
# healthy link is throttled by measurement noise, never unbounded.
CAP_MIN = 256 * 1024
CAP_MAX = 16 * 1024 * 1024
# Throughput smoothing: believe a faster drain quickly, a slower one slowly
# (a single slow sample is more often noise than a slower link).
_ALPHA_UP = 0.25
_ALPHA_DOWN = 0.1
# Latency feedback: halve on excess, recover by a quarter per good sample.
_SCALE_MIN = 0.125
_FLOOR_DRIFT_MS = 1.0  # per RTT sample; samples arrive about once a second


class BacklogGovernor:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._throughput = ASSUMED_BYTES_PER_SECOND
        self._last: tuple[float, int, int] | None = None  # (time, pending, sent_total)
        self._rtt_floor_ms: float | None = None
        self._latency_scale = 1.0

    @property
    def throughput(self) -> float:
        """Estimated drain rate of the viewer's socket, bytes per second."""
        return self._throughput

    @property
    def latency_scale(self) -> float:
        """1.0 while the round-trip time sits at its floor; smaller while
        excess latency says the queue is longer than the byte count shows."""
        return self._latency_scale

    @property
    def cap(self) -> int:
        """Unsent bytes a viewer may have queued before frames are withheld."""
        cap = self._throughput * QUEUE_TARGET_SECONDS * self._latency_scale
        return int(min(CAP_MAX, max(CAP_MIN, cap)))

    def observe(self, pending: int, sent_total: int) -> None:
        """Sample the socket: `pending` unsent bytes right now and the
        cumulative `sent_total` handed to it. Call once per broadcast tick."""
        now = self._clock()
        if self._last is not None:
            then, pending_then, sent_then = self._last
            elapsed = now - then
            # Only an interval the queue never emptied measures the link;
            # otherwise part of it was idle and the rate would read low.
            if pending_then > 0 and pending > 0 and elapsed > 0:
                drained = (sent_total - sent_then) - (pending - pending_then)
                rate = max(0.0, drained / elapsed)
                alpha = _ALPHA_UP if rate > self._throughput else _ALPHA_DOWN
                self._throughput += alpha * (rate - self._throughput)
        self._last = (now, pending, sent_total)

    def observe_rtt(self, rtt_ms: float) -> None:
        """Feed one round-trip-time sample (milliseconds)."""
        if self._rtt_floor_ms is None:
            self._rtt_floor_ms = rtt_ms
        else:
            self._rtt_floor_ms = min(rtt_ms, self._rtt_floor_ms + _FLOOR_DRIFT_MS)
        excess = rtt_ms - self._rtt_floor_ms
        target_ms = QUEUE_TARGET_SECONDS * 1000.0
        if excess > target_ms:
            self._latency_scale = max(_SCALE_MIN, self._latency_scale * 0.5)
        elif excess < target_ms / 2:
            self._latency_scale = min(1.0, self._latency_scale * 1.25)
