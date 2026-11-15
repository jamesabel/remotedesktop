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


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.fixture(scope="session", autouse=True)
def _tls_backend():
    """Run every test on the TLS backend the app pins at startup (schannel).

    Without this a test run started where OpenSSL DLLs are on PATH (Git Bash)
    gets Qt's openssl backend and access-violates on the first TLS connection.
    """
    tls.select_backend()


@pytest.fixture(scope="session")
def credentials(qapp):
    """One self-signed cert/key reused across tests (generation is ~100ms)."""
    return tls.ephemeral_credentials()


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.fixture(autouse=True)
def unanswered_dialogs(monkeypatch):
    """Keep a modal dialog that no test stubbed from blocking the run.

    A real QMessageBox.question, QMessageBox.exec or
    QFileDialog.getSaveFileName waits for a person. In CI nobody answers, so
    the job sits until the runner's limit: a failing loopback test once held
    the test job for six hours, because its cleanup closed a window with a
    viewer still connected, which asks "Quit?".

    Here such a dialog is declined at once (No / cancelled) and recorded, and
    the test then fails at teardown naming it. A test that expects a dialog
    stubs it with monkeypatch as before, which replaces these stand-ins.
    Yields the list of recorded dialogs.
    """
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    unanswered: list[str] = []

    def question(_parent=None, title="", text="", *_args, **_kwargs):
        unanswered.append(f"QMessageBox.question: {title}: {text}")
        return QMessageBox.StandardButton.No

    def message_box_exec(box):
        unanswered.append(f"QMessageBox.exec: {box.windowTitle()}: {box.text()}")
        return QMessageBox.StandardButton.No

    def get_save_file_name(_parent=None, caption="", *_args, **_kwargs):
        unanswered.append(f"QFileDialog.getSaveFileName: {caption}")
        return "", ""

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "exec", message_box_exec)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(get_save_file_name))
    yield unanswered
    assert not unanswered, f"modal dialog(s) shown with nobody to answer: {unanswered}"
