"""Keep the Qt event loop serviced during Windows' modal move/size loop.

Dragging (or click-and-holding) a window's title bar puts that window's
thread into a native modal loop; Qt's event dispatcher stops running, so
timers, socket reads — and therefore remote-input processing — all stall.
For the share server that was a deadlock: an injected remote mouse-down on
the server window's own title bar enters the loop, and the mouse-up that
would end it sits unread on a socket the frozen event loop never services,
until someone at the server machine intervenes.

Native WM_TIMER callbacks *are* dispatched inside modal loops, so from the
non-client mouse press (WM_NCLBUTTONDOWN — a press-and-hold that never
moves blocks in DefWindowProc's click tracking without ever sending
WM_ENTERSIZEMOVE) until the tracking loop releases mouse capture
(WM_CAPTURECHANGED, or WM_EXITSIZEMOVE after an actual drag), a SetTimer
callback pumps Qt events (user input excluded) every few milliseconds. A
side benefit: screen sharing keeps streaming while the local user drags
the server window.

The caption buttons (minimize/maximize/close) are the exception: their
tracking loop dispatches only mouse messages, so no timer — and therefore
no pump — can run inside it, and a remote click on the server's own
minimize button deadlocked just like the title-bar drag once did. Those
presses are instead handled here directly (`caption_action` callback) and
the message is consumed, so DefWindowProc's button tracking never starts:
the action happens on press, for local and remote clicks alike.

Feed every message from the window's `nativeEvent` to
`handle_native_event` and consume the message when it returns True; the
pump is inert off Windows. Tests inject a fake `timers` backend so no
native timer is ever created.
"""

import ctypes
import logging
import sys
from collections.abc import Callable
from ctypes import wintypes

from PySide6.QtCore import QCoreApplication, QEventLoop

_log = logging.getLogger("remotedesktop.modal_loop")

WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_CAPTURECHANGED = 0x0215
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
# WM_NCLBUTTONDOWN/-UP hit-test codes for the caption buttons.
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTCLOSE = 20
_CAPTION_BUTTONS = (HTMINBUTTON, HTMAXBUTTON, HTCLOSE)
_TIMER_ID = 0x5244  # arbitrary but stable; scoped to the window's hwnd
_TIMER_INTERVAL_MS = 15  # comfortably under the 33 ms capture tick


def _pump_qt() -> None:
    # Excluding user input keeps re-entrant clicks and keys out of our own
    # widgets while the native modal loop owns the mouse.
    QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)


class _NativeTimers:
    """SetTimer/KillTimer with a TIMERPROC, which the modal loop dispatches."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._user32 = ctypes.windll.user32
        # The WINFUNCTYPE wrapper must stay referenced for the timer's lifetime.
        self._proc = ctypes.WINFUNCTYPE(
            None, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.DWORD
        )(lambda _hwnd, _msg, _timer_id, _tick: callback())

    def start(self, hwnd: int) -> None:
        self._user32.SetTimer(hwnd, _TIMER_ID, _TIMER_INTERVAL_MS, self._proc)

    def stop(self, hwnd: int) -> None:
        self._user32.KillTimer(hwnd, _TIMER_ID)


class ModalLoopPump:
    """Runs a native timer that pumps Qt events while a window of ours sits
    in the modal move/size loop, and short-circuits caption-button clicks
    (whose tracking loop the timer cannot penetrate) to `caption_action`."""

    def __init__(
        self,
        *,
        pump: Callable[[], None] | None = None,
        caption_action: Callable[[int], None] | None = None,
        timers=None,
    ) -> None:
        self._pump = pump if pump is not None else _pump_qt
        self._caption_action = caption_action
        if timers is None and sys.platform == "win32":
            timers = _NativeTimers(self._on_timer)
        self._timers = timers  # None: inert (non-Windows)
        self._hwnd: int | None = None
        self._pumping = False

    def handle_native_event(self, event_type, message) -> bool:
        """Call from QWidget.nativeEvent with its arguments verbatim.

        Returns True when the message was handled here and must be consumed
        (returned True from nativeEvent) instead of reaching DefWindowProc.
        """
        if self._timers is None or bytes(event_type) != b"windows_generic_MSG":
            return False
        msg = wintypes.MSG.from_address(int(message))
        # Minimize/maximize/close presses must never reach DefWindowProc:
        # its caption-button tracking loop dispatches only mouse messages,
        # so no timer can pump Qt inside it, and a remote press deadlocks
        # exactly like the title-bar drag once did. Perform the action on
        # press and swallow the message (the matching -UP too).
        if (
            self._caption_action is not None
            and msg.message in (WM_NCLBUTTONDOWN, WM_NCLBUTTONUP)
            and msg.wParam in _CAPTION_BUTTONS
        ):
            if msg.message == WM_NCLBUTTONDOWN:
                _log.debug(
                    "Caption button %d pressed — handled without native tracking", msg.wParam
                )
                self._caption_action(int(msg.wParam))
            return True
        # WM_ENTERSIZEMOVE alone is not enough: a press-and-hold on the title
        # bar that never moves blocks the thread inside DefWindowProc's click
        # tracking WITHOUT ever sending WM_ENTERSIZEMOVE, so the pump must
        # arm on the non-client press itself. The tracking loop takes mouse
        # capture, so WM_CAPTURECHANGED marks its end whether or not a drag
        # (and its WM_EXITSIZEMOVE) ever happened.
        if msg.message in (WM_NCLBUTTONDOWN, WM_ENTERSIZEMOVE):
            self._enter(msg.hWnd or 0)
        elif msg.message in (WM_CAPTURECHANGED, WM_EXITSIZEMOVE):
            self._exit()
        return False

    def _enter(self, hwnd: int) -> None:
        if self._hwnd == hwnd:  # NCLBUTTONDOWN then ENTERSIZEMOVE: already armed
            return
        if self._hwnd is not None:  # unbalanced enter: replace the old timer
            self._timers.stop(self._hwnd)
        self._hwnd = hwnd
        self._timers.start(hwnd)
        _log.debug("Native mouse tracking started — pumping Qt from a native timer")

    def _exit(self) -> None:
        if self._hwnd is None:
            return
        self._timers.stop(self._hwnd)
        self._hwnd = None
        _log.debug("Native mouse tracking ended")

    def _on_timer(self) -> None:
        if self._pumping:  # processEvents can dispatch this timer re-entrantly
            return
        self._pumping = True
        try:
            self._pump()
        finally:
            self._pumping = False
