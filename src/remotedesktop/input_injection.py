"""Inject mouse and keyboard events into the local machine (server side).

Coordinates arrive normalized to 0..1 over the shared (primary) screen; the
Windows SendInput API takes absolute mouse coordinates as 0..65535 over the
primary monitor, so normalized input maps directly. Keyboard events carry the
client's native Windows virtual-key code, which is injected as-is.

On non-Windows platforms this is an inert stub so the rest of the app still
imports and runs (the app targets Windows).
"""

import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

# Qt mouse button name -> (down flag, up flag) for SendInput.
_MOUSE_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004),  # MOUSEEVENTF_LEFTDOWN / LEFTUP
    "right": (0x0008, 0x0010),  # MOUSEEVENTF_RIGHTDOWN / RIGHTUP
    "middle": (0x0020, 0x0040),  # MOUSEEVENTF_MIDDLEDOWN / MIDDLEUP
}
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_ABS_MAX = 65535
_MAPVK_VK_TO_VSC = 0

# Keys whose hardware scan code carries the 0xE0 prefix. Injecting these
# without KEYEVENTF_EXTENDEDKEY makes applications that read the extended
# bit (or scan codes) see the numpad variant instead.
_EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PgUp/PgDn/End/Home/arrows
    0x2C, 0x2D, 0x2E,  # PrintScreen/Insert/Delete
    0x5B, 0x5C, 0x5D,  # left/right Win, menu key
    0x6F,  # numpad divide
    0x90,  # NumLock
    0xA3, 0xA5,  # right Ctrl, right Alt
})


if _IS_WINDOWS:

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


def _clamp_abs(value: float) -> int:
    return max(0, min(_ABS_MAX, round(value * _ABS_MAX)))


class InputInjector:
    """Injects input on Windows via SendInput; a no-op elsewhere."""

    def __init__(self) -> None:
        self.available = _IS_WINDOWS

    def _send(self, structure) -> None:
        if not self.available:
            return
        ctypes.windll.user32.SendInput(1, ctypes.byref(structure), ctypes.sizeof(structure))

    def _mouse(self, flags: int, x: float | None = None, y: float | None = None, data: int = 0):
        dx = _clamp_abs(x) if x is not None else 0
        dy = _clamp_abs(y) if y is not None else 0
        if x is not None:
            flags |= _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
        mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None)
        return _INPUT(_INPUT_MOUSE, _INPUT_UNION(mi=mi))

    def move(self, x: float, y: float) -> None:
        if self.available:
            self._send(self._mouse(0, x, y))

    def button(self, x: float | None, y: float | None, name: str, pressed: bool) -> None:
        """Press or release a button; without coordinates, at the current cursor."""
        flags = _MOUSE_BUTTON_FLAGS.get(name)
        if flags is None or not self.available:
            return
        self._send(self._mouse(flags[0] if pressed else flags[1], x, y))

    def wheel(self, x: float, y: float, delta: int) -> None:
        if self.available:
            self._send(self._mouse(_MOUSEEVENTF_WHEEL, x, y, delta))

    def key(self, vk: int, pressed: bool) -> None:
        if not self.available or not vk:
            return
        vk &= 0xFFFF
        flags = 0 if pressed else _KEYEVENTF_KEYUP
        if vk in _EXTENDED_VKS:
            flags |= _KEYEVENTF_EXTENDEDKEY
        # Fill in the scan code too, for applications that read it.
        scan = ctypes.windll.user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
        ki = _KEYBDINPUT(vk, scan & 0xFF, flags, 0, None)
        self._send(_INPUT(_INPUT_KEYBOARD, _INPUT_UNION(ki=ki)))
