"""The pump must start its native timer exactly for the span of the modal
move/size loop, and its timer callback must never re-enter the Qt pump.

All tests inject a fake timer backend, so no real Win32 timer is created.
"""

import ctypes
from ctypes import wintypes

from remotedesktop.modal_loop import (
    WM_CAPTURECHANGED,
    WM_ENTERSIZEMOVE,
    WM_EXITSIZEMOVE,
    WM_NCLBUTTONDOWN,
    ModalLoopPump,
)


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


def test_click_and_hold_without_drag_is_pumped():
    # A press-and-hold on the title bar blocks in DefWindowProc's click
    # tracking without ever sending WM_ENTERSIZEMOVE; the pump must arm on
    # the press itself and disarm when the tracking loop releases capture.
    timers = FakeTimers()
    pump = ModalLoopPump(pump=lambda: None, timers=timers)
    press = native_message(WM_NCLBUTTONDOWN)
    release = native_message(WM_CAPTURECHANGED)
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(press))
    assert timers.calls == [("start", 0xBEEF)]
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(release))
    assert timers.calls == [("start", 0xBEEF), ("stop", 0xBEEF)]


def test_press_followed_by_real_drag_keeps_one_timer():
    # NCLBUTTONDOWN then ENTERSIZEMOVE (the hold turned into a drag) must not
    # restart the timer, and either end message stops it exactly once.
    timers = FakeTimers()
    pump = ModalLoopPump(pump=lambda: None, timers=timers)
    for message_id in (WM_NCLBUTTONDOWN, WM_ENTERSIZEMOVE):
        msg = native_message(message_id)
        pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(msg))
    assert timers.calls == [("start", 0xBEEF)]
    for message_id in (WM_CAPTURECHANGED, WM_EXITSIZEMOVE):
        msg = native_message(message_id)
        pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(msg))
    assert timers.calls == [("start", 0xBEEF), ("stop", 0xBEEF)]


def test_capture_change_without_press_is_ignored():
    # Qt widgets take and release mouse capture during ordinary client-area
    # clicks; a WM_CAPTURECHANGED with no pump running must do nothing.
    timers = FakeTimers()
    pump = ModalLoopPump(pump=lambda: None, timers=timers)
    msg = native_message(WM_CAPTURECHANGED)
    pump.handle_native_event(b"windows_generic_MSG", ctypes.addressof(msg))
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
