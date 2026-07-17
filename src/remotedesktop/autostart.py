"""Start the server automatically when the user logs in to Windows.

Uses the per-user Run registry key (HKCU), so no administrator rights are
needed and the server starts in the interactive session — which it needs,
because approving a new client is a GUI prompt. On non-Windows platforms
this is an inert stub, like input injection.
"""

import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    import winreg

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "remotedesktop-server"


def server_command() -> str:
    """The command line that launches this installation's server."""
    exe = Path(sys.executable).with_name("remotedesktop-server.exe")
    if exe.exists():
        return f'"{exe}"'
    # Fallback (e.g. running from source without the entry-point exe).
    return f'"{sys.executable}" -m remotedesktop.server'


class Autostart:
    """Reads and writes the login-autostart registration.

    Tests pass their own `key_path` so they never touch the real Run key.
    """

    def __init__(self, *, key_path: str = _RUN_KEY, value_name: str = _VALUE_NAME) -> None:
        self.available = _IS_WINDOWS
        self._key_path = key_path
        self._value_name = value_name

    def is_enabled(self) -> bool:
        if not self.available:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                winreg.QueryValueEx(key, self._value_name)
            return True
        except OSError:
            return False

    def set_enabled(self, enabled: bool) -> None:
        if not self.available:
            return
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._key_path) as key:
                winreg.SetValueEx(key, self._value_name, 0, winreg.REG_SZ, server_command())
        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, self._key_path, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, self._value_name)
            except OSError:
                pass  # already not registered
