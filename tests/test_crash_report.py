"""Tests for crash reporting.

All tests here are AI-authored. `alert` and `chain` are always injected, so
no test opens a message box or prints a traceback; hooks are never installed
globally except in the install test, which restores them.
"""

import logging
import sys
import threading

import pytest

from remotedesktop.crash_report import CrashReporter


def _raised(exc: BaseException):
    try:
        raise exc
    except type(exc) as caught:
        return type(caught), caught, caught.__traceback__


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.fixture
def reporter(tmp_path):
    alerts: list[str] = []
    chained: list[type[BaseException]] = []
    crash = CrashReporter(
        tmp_path / "remotedesktop.log",
        alert=alerts.append,
        chain=lambda exc_type, exc, tb: chained.append(exc_type),
    )
    return crash, alerts, chained


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_fatal_exception_is_logged_alerted_and_chained(reporter, caplog, tmp_path):
    crash, alerts, chained = reporter
    with caplog.at_level(logging.DEBUG, logger="remotedesktop.crash"):
        crash.excepthook(*_raised(ValueError("bad config")))
    assert len(alerts) == 1
    assert "ValueError: bad config" in alerts[0]
    assert str(tmp_path / "remotedesktop.log") in alerts[0]
    assert chained == [ValueError]
    [record] = caplog.records
    assert record.levelno == logging.CRITICAL
    assert record.exc_info[1].args == ("bad config",)


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_event_loop_exception_is_logged_only(reporter, caplog):
    crash, alerts, chained = reporter
    crash.fatal = False
    with caplog.at_level(logging.DEBUG, logger="remotedesktop.crash"):
        crash.excepthook(*_raised(KeyError("slot")))
    assert alerts == []
    assert chained == [KeyError]
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info[0] is KeyError


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_worker_thread_exception_is_logged_not_alerted(reporter, caplog):
    crash, alerts, chained = reporter

    def worker():
        raise RuntimeError("scan failed")

    thread = threading.Thread(target=worker, name="discovery-scan")
    previous = threading.excepthook
    threading.excepthook = crash.thread_excepthook
    try:
        with caplog.at_level(logging.DEBUG, logger="remotedesktop.crash"):
            thread.start()
            thread.join()
    finally:
        threading.excepthook = previous
    assert alerts == []
    assert chained == [RuntimeError]
    [record] = caplog.records
    assert "discovery-scan" in record.getMessage()
    assert record.exc_info[0] is RuntimeError


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_thread_system_exit_is_ignored(reporter, caplog):
    crash, alerts, chained = reporter

    def worker():
        raise SystemExit

    thread = threading.Thread(target=worker)
    previous = threading.excepthook
    threading.excepthook = crash.thread_excepthook
    try:
        with caplog.at_level(logging.DEBUG, logger="remotedesktop.crash"):
            thread.start()
            thread.join()
    finally:
        threading.excepthook = previous
    assert (alerts, chained, caplog.records) == ([], [], [])


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_install_replaces_both_hooks(reporter, monkeypatch):
    crash, _alerts, _chained = reporter
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    crash.install()
    assert sys.excepthook == crash.excepthook
    assert threading.excepthook == crash.thread_excepthook
