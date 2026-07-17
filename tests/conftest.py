import pytest
from PySide6.QtWidgets import QApplication

from remotedesktop import db, tls


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


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
