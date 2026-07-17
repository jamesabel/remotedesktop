"""Persistent debug logging for the client and server apps.

The GUI "Connection log" panes only live as long as their window, which makes
an intermittent connection problem impossible to diagnose after the fact.
`init_logging` attaches a rotating file handler to the "remotedesktop" logger
so everything the panes show — plus lower-level socket and protocol detail
that is logged directly — survives on disk with millisecond timestamps.

Only the app entry points call `init_logging`; library code and tests just
log to `logging.getLogger("remotedesktop...")`, which is a no-op without a
handler.
"""

import logging
import logging.handlers
from pathlib import Path

import platformdirs


def default_log_dir() -> Path:
    return Path(platformdirs.user_log_dir("remotedesktop", appauthor=False))


def init_logging(app_name: str, *, directory: Path | None = None) -> Path:
    """Write "remotedesktop" logger output to <log dir>/<app_name>.log.

    Returns the log file path. `directory` exists for tests; the apps use
    the platformdirs log dir.
    """
    log_dir = directory if directory is not None else default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{app_name}.log"
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger = logging.getLogger("remotedesktop")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return path
