import gc
import threading

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from remotedesktop import db, tls
from remotedesktop.app import MainWindow

# Worker threads the app starts; each must be gone by the end of the test
# that started it, or it can outlive its Qt objects.
_APP_THREAD_NAMES = ("discovery-responder", "discovery-scan", "reconnect-rediscovery")


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


def pump_deferred_deletes(app: QApplication, rounds: int = 3) -> None:
    """Deliver pending events, including every queued deleteLater()."""
    for _round in range(rounds):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def _deterministic_qt_teardown(qapp):
    """Destroy every Qt object a test leaves behind, at a known point.

    Tests drop windows without deleting them, and a MainWindow is a reference
    cycle, so without this the C++ objects die whenever the cyclic GC happens
    to run — possibly mid-test, on a worker thread, or while the event loop
    isn't being pumped, which is how PySide aborts / access-violates at
    random. Closing, deleteLater-ing, pumping and collecting here moves all
    that destruction to the end of the test, on the GUI thread, with the event
    loop running. The thread check then guards the other half: no app worker
    thread may outlive its test.
    """
    yield
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, MainWindow):
            # Bypass the "hide to tray while sharing" and "N viewers connected"
            # close paths — a test fixture must neither hide nor prompt.
            widget.sharing_tab.shutdown()
            widget._quitting = True
        widget.close()
        widget.deleteLater()
    pump_deferred_deletes(qapp)
    gc.collect()
    pump_deferred_deletes(qapp)
    leftover = [t for t in threading.enumerate() if t.name in _APP_THREAD_NAMES]
    for thread in leftover:
        thread.join(timeout=5.0)
    alive = [t.name for t in leftover if t.is_alive()]
    assert not alive, f"app worker thread(s) outlived the test: {alive}"


@pytest.fixture(autouse=True)
def _close_db_connections(monkeypatch):
    """Close every SQLite connection a test opens via db.connect.

    Tests open connections freely (stores, windows, reopened files) and
    nothing else owns their lifetime, which leaks ResourceWarnings at GC
    time. Track them per-test and close them all at teardown; closing an
    already-closed connection is a no-op.
    """
    connections = []
    real_connect = db.connect

    def tracking_connect(path):
        connection = real_connect(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "connect", tracking_connect)
    yield
    for connection in connections:
        connection.close()


@pytest.fixture(scope="session")
def credentials(qapp):
    """One self-signed cert/key reused across tests (generation is ~100ms)."""
    return tls.ephemeral_credentials()
