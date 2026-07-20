"""Detect whether the Windows secure desktop is in control (server side).

While the workstation is locked — and during UAC prompts and the
Ctrl+Alt+Del screen — Windows switches to a separate "secure desktop" that
processes running as the signed-in user may neither capture nor inject
input into. That is a deliberate security boundary (nothing in the user's
session can read or type into a credential prompt), so remote unlock is
impossible for this app by design: screen capture yields a stale frame and
SendInput is silently discarded. The server uses this probe to tell
viewers what is going on instead of leaving them a frozen screen.

The check: `OpenInputDesktop` fails for an ordinary user process while the
input desktop is the secure desktop, and succeeds while the user's own
desktop has input.

On non-Windows platforms `is_session_locked()` returns None (the app
targets Windows).
"""

import ctypes
import sys

_IS_WINDOWS = sys.platform == "win32"

# winuser.h desktop access right — the weakest thing OpenInputDesktop can be
# asked for, enough to answer "may this process touch the input desktop?".
_DESKTOP_SWITCHDESKTOP = 0x0100

if _IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _user32.OpenInputDesktop.restype = ctypes.c_void_p
    _user32.OpenInputDesktop.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    _user32.CloseDesktop.argtypes = (ctypes.c_void_p,)


def is_session_locked() -> bool | None:
    """True while the secure desktop (lock screen, UAC prompt) has the
    input, False while the user's own desktop does, or None when it cannot
    be determined (non-Windows)."""
    if not _IS_WINDOWS:
        return None
    handle = _user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
    if not handle:
        return True
    _user32.CloseDesktop(handle)
    return False
