"""Single-instance guard tests, on unique socket names so parallel test
runs (and any real running app) are never touched."""

import uuid

from remotedesktop.single_instance import SingleInstance

from test_sharing import pump


def unique_name() -> str:
    return f"remotedesktop-test-{uuid.uuid4()}"


def test_first_acquire_wins(qapp):
    guard = SingleInstance(unique_name())
    try:
        assert guard.acquire()
    finally:
        guard.release()


def test_second_acquire_yields_and_activates_the_first(qapp):
    name = unique_name()
    first = SingleInstance(name)
    activated = []
    first.activateRequested.connect(lambda: activated.append(True))
    assert first.acquire()
    second = SingleInstance(name)
    try:
        assert not second.acquire()  # the second launch must exit...
        pump(qapp, lambda: activated)  # ...after waking the first instance
    finally:
        first.release()


def test_name_is_reusable_after_release(qapp):
    name = unique_name()
    first = SingleInstance(name)
    assert first.acquire()
    first.release()
    second = SingleInstance(name)
    try:
        assert second.acquire()
    finally:
        second.release()
