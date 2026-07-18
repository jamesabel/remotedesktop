from PySide6.QtWidgets import QMainWindow

from remotedesktop import db, window_state
from remotedesktop.config import Settings


def test_geometry_round_trips_through_settings(qapp, tmp_path):
    path = tmp_path / "app.db"
    window = QMainWindow()
    window.resize(777, 555)
    window_state.save_geometry(window, Settings(db.connect(path)), "geom")

    # A fresh window on a fresh connection gets the saved size back.
    restored = QMainWindow()
    window_state.restore_geometry(restored, Settings(db.connect(path)), "geom")
    assert (restored.width(), restored.height()) == (777, 555)


def test_restore_without_saved_geometry_leaves_window_alone(qapp):
    settings = Settings(db.connect(None))
    window = QMainWindow()
    window.resize(400, 300)
    window_state.restore_geometry(window, settings, "missing")
    assert (window.width(), window.height()) == (400, 300)


def test_main_geometry_key_exists():
    assert window_state.MAIN_GEOMETRY_KEY == "main_window_geometry"
