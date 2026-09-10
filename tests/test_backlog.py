from remotedesktop import backlog
from remotedesktop.backlog import BacklogGovernor


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = 1 / 30) -> None:
        self.now += seconds


def drain_at(governor: BacklogGovernor, clock: Clock, bytes_per_second: float, ticks: int) -> None:
    """Simulate a link draining a never-empty queue at a steady rate: each
    tick we hand the socket more than it can send, so `pending` stays > 0."""
    sent = 0
    pending = 1
    governor.observe(pending, sent)
    for _tick in range(ticks):
        clock.tick()
        per_tick = bytes_per_second / 30
        sent += int(per_tick * 2)  # enqueue twice what drains
        pending += int(per_tick * 2) - int(per_tick)  # drain per_tick
        governor.observe(pending, sent)


def test_starts_from_the_100_mbit_assumption() -> None:
    governor = BacklogGovernor(clock=Clock())
    assert governor.throughput == backlog.ASSUMED_BYTES_PER_SECOND
    assert governor.cap == int(backlog.ASSUMED_BYTES_PER_SECOND * backlog.QUEUE_TARGET_SECONDS)


def test_a_fast_link_raises_the_cap_within_a_few_busy_ticks() -> None:
    clock = Clock()
    governor = BacklogGovernor(clock=clock)
    gigabit = 110 * 1024 * 1024  # realistic payload rate on 1 Gbit/s
    drain_at(governor, clock, gigabit, ticks=15)
    assert governor.throughput > 0.9 * gigabit
    assert governor.cap > 4 * 1024 * 1024


def test_a_slow_link_lowers_the_cap_but_never_below_the_floor() -> None:
    clock = Clock()
    governor = BacklogGovernor(clock=clock)
    drain_at(governor, clock, 1 * 1024 * 1024, ticks=90)  # a bad Wi-Fi link
    assert governor.throughput < 2 * 1024 * 1024
    assert governor.cap == backlog.CAP_MIN


def test_the_cap_is_bounded_above() -> None:
    clock = Clock()
    governor = BacklogGovernor(clock=clock)
    drain_at(governor, clock, 10 * 1024 * 1024 * 1024, ticks=30)
    assert governor.cap == backlog.CAP_MAX


def test_an_interval_in_which_the_queue_emptied_is_not_a_sample() -> None:
    clock = Clock()
    governor = BacklogGovernor(clock=clock)
    governor.observe(pending=0, sent_total=0)
    clock.tick()
    governor.observe(pending=0, sent_total=10_000_000)  # idle either end: link unknown
    assert governor.throughput == backlog.ASSUMED_BYTES_PER_SECOND


def test_excess_latency_halves_the_cap_and_it_recovers() -> None:
    governor = BacklogGovernor(clock=Clock())
    base = governor.cap
    for _sample in range(3):
        governor.observe_rtt(2.0)  # establishes the floor
    governor.observe_rtt(120.0)  # pings queued behind frame data
    assert governor.latency_scale == 0.5
    assert governor.cap == base // 2
    governor.observe_rtt(150.0)
    governor.observe_rtt(150.0)
    governor.observe_rtt(150.0)
    assert governor.latency_scale == backlog._SCALE_MIN  # never collapses to zero
    for _sample in range(20):
        governor.observe_rtt(3.0)
    assert governor.latency_scale == 1.0
    assert governor.cap == base


def test_the_rtt_floor_drifts_up_to_follow_a_changed_baseline() -> None:
    governor = BacklogGovernor(clock=Clock())
    governor.observe_rtt(1.0)
    # The link's idle RTT becomes 40 ms for good (e.g. a different route):
    # the first samples look like excess, but the floor catches up at
    # 1 ms per sample and the scale recovers instead of staying pinned.
    for _sample in range(120):
        governor.observe_rtt(40.0)
    assert governor.latency_scale == 1.0
