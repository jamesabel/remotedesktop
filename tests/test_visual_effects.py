import sys

import pytest

from remotedesktop.visual_effects import (
    _BOOL_KNOBS,
    REDUCED_MENU_DELAY_MS,
    SpiBackend,
    VisualEffectsReducer,
)

_GET_TO_SET = {get_code: set_code for get_code, set_code, _ in _BOOL_KNOBS}


class FakeSpiBackend:
    """Records set calls instead of calling SystemParametersInfo.

    Tests must NEVER call a real SpiBackend's setters — that would change
    the host machine's actual Windows visual-effect settings.
    """

    def __init__(self, *, bools=True, min_animate=True, menu_delay=400):
        self.available = True
        self.bools = {set_code: bools for _, set_code, _ in _BOOL_KNOBS}
        self.min_animate = min_animate
        self.menu_delay = menu_delay
        self.set_calls: list[tuple] = []

    def get_bool(self, code):
        return self.bools[_GET_TO_SET[code]]

    def set_bool(self, code, value):
        self.bools[code] = value
        self.set_calls.append(("bool", code, value))

    def get_min_animate(self):
        return self.min_animate

    def set_min_animate(self, value):
        self.min_animate = value
        self.set_calls.append(("min_animate", value))

    def get_menu_delay(self):
        return self.menu_delay

    def set_menu_delay(self, milliseconds):
        self.menu_delay = milliseconds
        self.set_calls.append(("menu_delay", milliseconds))


def test_apply_reduces_and_restore_returns_exact_previous_values():
    backend = FakeSpiBackend(bools=True, min_animate=True, menu_delay=400)
    reducer = VisualEffectsReducer(backend)
    assert reducer.apply()
    assert reducer.applied
    assert all(value is False for value in backend.bools.values())
    assert backend.min_animate is False
    assert backend.menu_delay == REDUCED_MENU_DELAY_MS
    assert reducer.restore()
    assert not reducer.applied
    assert all(value is True for value in backend.bools.values())
    assert backend.min_animate is True
    assert backend.menu_delay == 400


def test_already_reduced_settings_are_never_touched():
    # A user who already disabled the effects keeps their own settings:
    # nothing is set on apply, so nothing is "restored" over them either.
    backend = FakeSpiBackend(bools=False, min_animate=False, menu_delay=20)
    reducer = VisualEffectsReducer(backend)
    assert reducer.apply()  # transitions to applied, but changes nothing
    assert backend.set_calls == []
    assert reducer.restore()
    assert backend.set_calls == []
    assert backend.menu_delay == 20  # a below-target delay is never raised


def test_apply_and_restore_are_idempotent():
    backend = FakeSpiBackend()
    reducer = VisualEffectsReducer(backend)
    assert not reducer.restore()  # nothing applied yet
    assert reducer.apply()
    calls = len(backend.set_calls)
    assert not reducer.apply()  # second apply: no-op, nothing re-saved
    assert len(backend.set_calls) == calls
    assert reducer.restore()
    assert not reducer.restore()
    # A full second cycle still works after restoring.
    assert reducer.apply() and reducer.restore()
    assert backend.min_animate is True


def test_unreadable_knob_is_skipped_not_restored():
    class UnreadableAnimateBackend(FakeSpiBackend):
        def get_min_animate(self):  # API failure for this knob
            return None

    backend = UnreadableAnimateBackend()
    reducer = VisualEffectsReducer(backend)
    assert reducer.apply()
    assert reducer.restore()
    assert ("min_animate", False) not in backend.set_calls
    assert not any(call[0] == "min_animate" for call in backend.set_calls)


def test_unavailable_backend_is_inert():
    backend = FakeSpiBackend()
    backend.available = False
    reducer = VisualEffectsReducer(backend)
    assert not reducer.available
    assert not reducer.apply()
    assert not reducer.restore()
    assert backend.set_calls == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows SPI API")
def test_real_backend_reads_current_values():
    # Read-only: never call the real backend's setters from a test.
    backend = SpiBackend()
    assert backend.available
    for get_code, _set_code, _label in _BOOL_KNOBS:
        assert backend.get_bool(get_code) in (True, False, None)
    assert backend.get_min_animate() in (True, False, None)
    delay = backend.get_menu_delay()
    assert delay is None or delay >= 0
