import pytest
from PySide6.QtWidgets import QApplication

from remotedesktop import tls


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    return app if isinstance(app, QApplication) else QApplication([])


@pytest.fixture(scope="session")
def credentials(qapp):
    """One self-signed cert/key reused across tests (generation is ~100ms)."""
    return tls.ephemeral_credentials()
