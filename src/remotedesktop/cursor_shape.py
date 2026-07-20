"""Identify the shape of the mouse cursor currently shown (server side).

The captured frame deliberately contains no mouse pointer (both DXGI desktop
duplication and GDI BitBlt exclude it), so what the viewer's user sees over
the remote screen is their own local cursor. This module lets the server
report what shape that cursor should be — an I-beam over text, the resize
arrows over a window edge, and so on: GetCursorInfo returns the current
cursor handle, and the standard cursors are shared handles identical to what
LoadCursor(NULL, IDC_*) returns, so a plain handle comparison identifies
them. Custom application cursors fall back to "arrow"; a cursor that is not
showing at all reports "hidden".

On non-Windows platforms `current_cursor_shape()` returns None (the app
targets Windows).
"""

import ctypes
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

_CURSOR_SHOWING = 0x1

# Standard cursor resource ids (winuser.h IDC_*) -> wire shape names. These
# names are the protocol vocabulary: ViewerWidget maps them to Qt cursors.
_IDC_SHAPES = {
    32512: "arrow",  # IDC_ARROW
    32513: "ibeam",  # IDC_IBEAM
    32514: "wait",  # IDC_WAIT
    32515: "cross",  # IDC_CROSS
    32516: "uparrow",  # IDC_UPARROW
    32642: "size_nwse",  # IDC_SIZENWSE (diagonal resize ↘)
    32643: "size_nesw",  # IDC_SIZENESW (diagonal resize ↗)
    32644: "size_we",  # IDC_SIZEWE (horizontal resize)
    32645: "size_ns",  # IDC_SIZENS (vertical resize)
    32646: "size_all",  # IDC_SIZEALL
    32648: "no",  # IDC_NO
    32649: "hand",  # IDC_HAND
    32650: "appstarting",  # IDC_APPSTARTING
    32651: "help",  # IDC_HELP
}

SHAPE_NAMES = frozenset(_IDC_SHAPES.values()) | {"hidden"}


if _IS_WINDOWS:

    class _CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", ctypes.c_void_p),
            ("ptScreenPos", wintypes.POINT),
        ]


_handle_shapes: dict[int, str] | None = None


def _standard_handles() -> dict[int, str]:
    """HCURSOR -> shape name for every standard cursor (loaded once; the
    handles are shared and stable for the lifetime of the process)."""
    global _handle_shapes
    if _handle_shapes is None:
        user32 = ctypes.windll.user32
        user32.LoadCursorW.restype = ctypes.c_void_p
        user32.LoadCursorW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        _handle_shapes = {}
        for resource, name in _IDC_SHAPES.items():
            # MAKEINTRESOURCE: the id passed as the pointer-sized name arg.
            handle = user32.LoadCursorW(None, ctypes.c_void_p(resource))
            if handle:
                _handle_shapes[handle] = name
    return _handle_shapes


def current_cursor_shape() -> str | None:
    """The current cursor's shape name, "hidden" while no cursor is showing,
    or None when it cannot be determined (non-Windows, API failure)."""
    if not _IS_WINDOWS:
        return None
    info = _CURSORINFO()
    info.cbSize = ctypes.sizeof(_CURSORINFO)
    if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(info)):
        return None
    if not info.flags & _CURSOR_SHOWING:
        return "hidden"
    return _standard_handles().get(info.hCursor or 0, "arrow")
