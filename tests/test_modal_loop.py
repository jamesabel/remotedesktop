"""The pump must start its native timer exactly for the span of the modal
move/size loop, and its timer callback must never re-enter the Qt pump.

All tests inject a fake timer backend, so no real Win32 timer is created.
"""

import ctypes
from ctypes import wintypes

from remotedesktop.modal_loop import WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, ModalLoopPump


class FakeTimers:
    def __init__(self):
        self.calls = []

    def start(self, hwnd):
        self.calls.append(("start", hwnd))

    def stop(self, hwnd):
        self.calls.append(("stop", hwnd))


def native_message(message_id, hwnd=0xBEEF):
    """A wintypes.MSG whose address stands in for Qt's nativeEvent pointer."""
    msg = wintypes.MSG()
    msg.hWnd = hwnd
    msg.message = message_id
    return msg


def test_enter_and_exit_bound_the_timer():
    timers = FakeTimers()
    pump = ModalLoopPump(pump=lambda: None, timers=timers)
    enter, exit_ = native_message(WM_ENTERSIZEMOVE), native_message(WM_EXITSIZEMOVE)
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(enter))
    assert timers.calls == [("start", 0xBEEF)]
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(exit_))
    assert timers.calls == [("start", 0xBEEF), ("stop", 0xBEEF)]


def test_exit_without_enter_is_ignored():
    timers = FakeTimers()
    pump = ModalLoopPump(pump=lambda: None, timers=timers)
    exit_ = native_message(WM_EXITSIZEMOVE)
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(exit_))
    assert timers.calls == []


def test_other_messages_and_event_types_are_ignored():
    timers = FakeTimers()
    pump = ModalLoopPump(pump=lambda: None, timers=timers)
    other = native_message(0x000F)  # WM_PAINT
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(other))
    # A non-Windows event type must not even be parsed as a MSG pointer.
    pump.handle_native_event(b"xcb_generic_event_t", 0)
    assert timers.calls == []


def test_timer_callback_pumps_but_never_reentrantly():
    pumped = []
    pump = ModalLoopPump(pump=lambda: pumped.append(True) or pump._on_timer(), timers=FakeTimers())
    # The pump callable above simulates processEvents dispatching our own
    # WM_TIMER again mid-pump; the guard must swallow that inner call.
    pump._on_timer()
    assert pumped == [True]
    pump._on_timer()  # guard resets between ticks
    assert pumped == [True, True]
