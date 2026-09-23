"""Hide a console window the app was never meant to have.

The app is started windowless — the `remotedesktop` gui-script, autostart,
and restart all run it under `pythonw.exe`. A broken venv can still hand
it a console: uv 0.11 built venvs whose `Scripts\\pythonw.exe` was a copy of
the console `python.exe` launcher, so every login opened a stray console
window next to the app. Rebuilding the venv is the real fix
(`broken_pythonw` detects the fault so the app can say so); hiding the
window is the safety net.

The rule is explicit: running as `pythonw.exe` means "no console wanted",
so any console window present is hidden (or, under Windows Terminal, which
won't hide, minimized). `python -m remotedesktop` from a
terminal (the way to see stdout/tracebacks) is left alone.

On non-Windows platforms this is an inert stub.
"""

import ctypes
import struct
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"

# winuser.h ShowWindow commands
_SW_HIDE = 0
_SW_MINIMIZE = 6

_IMAGE_SUBSYSTEM_WINDOWS_CUI = 3  # PE optional-header subsystem: a console program

if _IS_WINDOWS:
    _kernel32 = ctypes.windll.kernel32
    _kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    _kernel32.GetConsoleWindow.argtypes = ()
    _user32 = ctypes.windll.user32
    _user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)


def wants_no_console(executable: str) -> bool:
    """True when the app runs under an interpreter meant to be windowless."""
    return Path(executable).name.lower() == "pythonw.exe"


def is_console_program(exe: Path) -> bool:
    """True when `exe`'s PE header says it is a console program.

    False when the file can't be read or isn't a PE image.
    """
    try:
        with exe.open("rb") as file:
            header = file.read(4096)
    except OSError:
        return False
    if len(header) < 0x40 or header[:2] != b"MZ":
        return False
    pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
    # PE signature (4) + COFF header (20), then the optional header's
    # Subsystem field at offset 68.
    subsystem_offset = pe_offset + 24 + 68
    if header[pe_offset : pe_offset + 4] != b"PE\0\0" or len(header) < subsystem_offset + 2:
        return False
    (subsystem,) = struct.unpack_from("<H", header, subsystem_offset)
    return subsystem == _IMAGE_SUBSYSTEM_WINDOWS_CUI


def broken_pythonw(executable: str | None = None) -> Path | None:
    """The `pythonw.exe` beside this interpreter, if it is really a console
    program (the uv 0.11 fault) — every windowless launch from this
    environment then opens a console. None when it is fine or absent.
    """
    pythonw = Path(sys.executable if executable is None else executable).with_name("pythonw.exe")
    return pythonw if is_console_program(pythonw) else None


def hide_unwanted_console(executable: str | None = None) -> bool:
    """Hide this process's console window if it runs as pythonw.

    Returns True when a console window was found and hidden/minimized.
    """
    if not _IS_WINDOWS:
        return False
    if not wants_no_console(sys.executable if executable is None else executable):
        return False
    hwnd = _kernel32.GetConsoleWindow()
    if not hwnd:
        return False  # the normal case: pythonw has no console
    # Minimize first, then hide: the classic console host (conhost) honors
    # both and ends up hidden; Windows Terminal ignores hide requests on its
    # pseudo-console window but honors minimize, so it ends up minimized.
    _user32.ShowWindow(hwnd, _SW_MINIMIZE)
    _user32.ShowWindow(hwnd, _SW_HIDE)
    return True
