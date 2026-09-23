"""Report unhandled exceptions from a windowless app.

Under `pythonw.exe` (the gui-script, autostart at login, restart) there is
no console, so an unhandled exception — the default hook prints it to a
stderr that goes nowhere — would make the app silently fail to appear.
`CrashReporter` replaces `sys.excepthook` and `threading.excepthook`:

- every unhandled exception is written to the debug log with its traceback;
- while the app is still starting (before the event loop runs) or after it
  has stopped, the exception is fatal, so a message box also says what
  happened and where the log is;
- inside the event loop, an exception escaping a Qt slot doesn't stop the
  app (Qt carries on), so it is only logged — a failing timer slot must not
  stack up modal boxes. Worker-thread exceptions are likewise log-only.

The default hook still runs afterwards, so `python -m remotedesktop` from a
terminal prints tracebacks as before.
"""

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

_log = logging.getLogger("remotedesktop.crash")

_MB_ICONERROR = 0x10
_MB_SETFOREGROUND = 0x10000
_MB_TOPMOST = 0x40000


def show_error(message: str) -> None:
    """A native message box — works even if Qt itself failed to start."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "Remote Desktop",
            _MB_ICONERROR | _MB_SETFOREGROUND | _MB_TOPMOST,
        )


class CrashReporter:
    """Log unhandled exceptions; alert on the fatal ones.

    `fatal` is True until the host sets it False as the event loop starts,
    and back to True once it returns. `alert` and `chain` are injectable so
    tests never open a message box or print tracebacks.
    """

    def __init__(
        self,
        log_path: Path,
        *,
        alert: Callable[[str], None] = show_error,
        chain: Callable[
            [type[BaseException], BaseException, TracebackType | None], object
        ] = sys.__excepthook__,
    ) -> None:
        self.log_path = log_path
        self.fatal = True
        self._alert = alert
        self._chain = chain

    def install(self) -> None:
        sys.excepthook = self.excepthook
        threading.excepthook = self.thread_excepthook

    def excepthook(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if self.fatal:
            _log.critical("Unhandled exception; the app cannot continue", exc_info=(exc_type, exc, tb))
            self._alert(
                "Remote Desktop stopped because of an unexpected error:\n\n"
                f"{exc_type.__name__}: {exc}\n\n"
                f"Details are in the log:\n{self.log_path}"
            )
        else:
            _log.error("Unhandled exception in an event handler", exc_info=(exc_type, exc, tb))
        self._chain(exc_type, exc, tb)

    def thread_excepthook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return  # a thread exiting on purpose, as threading's own hook treats it
        name = args.thread.name if args.thread is not None else "unknown"
        exc = args.exc_value if args.exc_value is not None else args.exc_type()
        _log.error(
            "Unhandled exception in thread %s",
            name,
            exc_info=(args.exc_type, exc, args.exc_traceback),
        )
        self._chain(args.exc_type, exc, args.exc_traceback)
