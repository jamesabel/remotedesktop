"""Start the app automatically when the user logs in to Windows.

Uses the per-user Run registry key (HKCU), so no administrator rights are
needed and the app starts in the interactive session — which it needs,
because approving a new client is a GUI prompt. Login-started instances get
`--minimized`, so an instance that is sharing goes straight to the tray.
On non-Windows platforms this is an inert stub, like input injection.

Installations upgraded from the separate server app may still carry the old
"remotedesktop-server" Run value; `migrate_legacy()` (called once at app
startup) moves that registration to the new value name and command.
"""

import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    import winreg

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "remotedesktop"
_LEGACY_VALUE_NAME = "remotedesktop-server"  # the pre-1.0 server-only app


def app_command() -> str:
    """The command line that launches this installation at login."""
    exe = Path(sys.executable).with_name("remotedesktop.exe")
    if exe.exists():
        return f'"{exe}" --minimized'
    # Fallback (e.g. running from source without the entry-point exe).
    return f'"{sys.executable}" -m remotedesktop --minimized'


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

    def _has_value(self, value_name: str) -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                winreg.QueryValueEx(key, value_name)
            return True
        except OSError:
            return False

    def _delete_value(self, value_name: str) -> None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._key_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, value_name)
        except OSError:
            pass  # already not registered

    def is_enabled(self) -> bool:
        if not self.available:
            return False
        return self._has_value(self._value_name)

    def set_enabled(self, enabled: bool) -> None:
        if not self.available:
            return
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                winreg.SetValueEx(key, self._value_name, 0, winreg.REG_SZ, app_command())
            self._delete_value(self._legacy_value_name)  # never leave both
        else:
            self._delete_value(self._value_name)

    def migrate_legacy(self) -> None:
        """Move a pre-1.0 "remotedesktop-server" registration to this app.

        Best-effort: a registry failure must never block startup.
        """
        if not self.available:
            return
        try:
            if self._has_value(self._legacy_value_name):
                self.set_enabled(True)
        except OSError:
            pass
