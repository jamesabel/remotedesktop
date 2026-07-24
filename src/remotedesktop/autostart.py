"""Start the app automatically when the user logs in to Windows.

Uses the per-user Run registry key (HKCU), so no administrator rights are
needed and the app starts in the interactive session — which it needs,
because approving a new client is a GUI prompt. The registration is one of
four *start modes*: minimized (the default and recommended — a sharing
instance goes straight to the tray), a normal window, maximized, or off
(no registration). The mode is encoded in the registered command line
(`--minimized`, no flag, `--maximized`), so the Run value alone says what
happens at login. On non-Windows platforms this is an inert stub, like
input injection.

Installations upgraded from the separate server app may still carry the old
"remotedesktop-server" Run value; `migrate_legacy()` (called once at app
startup) moves that registration to the new value name and command.
"""

import re
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    import winreg

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "remotedesktop"
_LEGACY_VALUE_NAME = "remotedesktop-server"  # the pre-1.0 server-only app

# How (and whether) the app starts at login.
START_MINIMIZED = "minimized"  # the default and recommended mode
START_NORMAL = "normal"
START_MAXIMIZED = "maximized"
START_OFF = "off"
START_MODES = (START_MINIMIZED, START_NORMAL, START_MAXIMIZED, START_OFF)

# The command-line suffix that makes a login-started instance open that way.
_MODE_FLAGS = {
    START_MINIMIZED: " --minimized",
    START_NORMAL: "",
    START_MAXIMIZED: " --maximized",
}

# A pyship CLIP directory: <install dir>\remotedesktop_<version>\pythonw.exe.
_CLIP_DIR_RE = re.compile(r"remotedesktop_\d+(\.\d+)*", re.IGNORECASE)


def installed_launcher() -> Path | None:
    """The pyship launcher exe, when running from an installed CLIP.

    The launcher always starts the newest installed remotedesktop_<version>
    directory, so registrations and relaunches that go through it survive
    upgrades — a path into this CLIP would go stale instead.
    """
    exe_dir = Path(sys.executable).parent
    if _CLIP_DIR_RE.fullmatch(exe_dir.name):
        launcher = exe_dir.parent / "remotedesktop" / "remotedesktop.exe"
        if launcher.exists():
            return launcher
    return None


def app_command(mode: str = START_MINIMIZED) -> str:
    """The command line that launches this installation at login."""
    flag = _MODE_FLAGS[mode]
    launcher = installed_launcher()
    if launcher is not None:
        return f'"{launcher}"{flag}'
    exe = Path(sys.executable).with_name("remotedesktop.exe")  # venv Scripts dir
    if exe.exists():
        return f'"{exe}"{flag}'
    # Fallback (e.g. running from source without the entry-point exe).
    return f'"{sys.executable}" -m remotedesktop{flag}'


class Autostart:
    """Reads and writes the login-autostart registration.

    Tests pass their own `key_path` so they never touch the real Run key.
    """

    def __init__(
        self,
        *,
        key_path: str = _RUN_KEY,
        value_name: str = _VALUE_NAME,
        legacy_value_name: str = _LEGACY_VALUE_NAME,
    ) -> None:
        self.available = _IS_WINDOWS
        self._key_path = key_path
        self._value_name = value_name
        self._legacy_value_name = legacy_value_name

    def _read_value(self, value_name: str) -> str | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                value, _kind = winreg.QueryValueEx(key, value_name)
            return str(value)
        except OSError:
            return None

    def _has_value(self, value_name: str) -> bool:
        return self._read_value(value_name) is not None

    def _delete_value(self, value_name: str) -> None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._key_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, value_name)
        except OSError:
            pass  # already not registered

    def mode(self) -> str:
        """The registered start mode, read back from the Run value's flag."""
        if not self.available:
            return START_OFF
        command = self._read_value(self._value_name)
        if command is None:
            return START_OFF
        if command.endswith("--minimized"):
            return START_MINIMIZED
        if command.endswith("--maximized"):
            return START_MAXIMIZED
        return START_NORMAL

    def set_mode(self, mode: str) -> None:
        if not self.available:
            return
        if mode == START_OFF:
            self._delete_value(self._value_name)
            return
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
            winreg.SetValueEx(key, self._value_name, 0, winreg.REG_SZ, app_command(mode))
        self._delete_value(self._legacy_value_name)  # never leave both

    def migrate_legacy(self) -> None:
        """Move a pre-1.0 "remotedesktop-server" registration to this app.

        Best-effort: a registry failure must never block startup.
        """
        if not self.available:
            return
        try:
            if self._has_value(self._legacy_value_name):
                self.set_mode(START_MINIMIZED)
        except OSError:
            pass
