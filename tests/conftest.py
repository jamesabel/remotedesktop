import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])
