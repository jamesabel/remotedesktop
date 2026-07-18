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


def test_main_settings_keys_exist():
    assert window_state.MAIN_GEOMETRY_KEY == "main_window_geometry"
    assert window_state.MAIN_STATE_KEY == "main_window_state"


def test_dock_layout_round_trips_through_settings(qapp, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDockWidget

    def window_with_dock():
        window = QMainWindow()
        dock = QDockWidget("Servers", window)
        dock.setObjectName("dock")  # required for saveState to include it
        window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        return window, dock

    path = tmp_path / "app.db"
    window, dock = window_with_dock()
    window.show()
    assert dock.isVisible()
    dock.close()
    window_state.save_state(window, Settings(db.connect(path)), "state")
    window.close()

    restored, restored_dock = window_with_dock()
    restored.show()
    assert restored_dock.isVisible()  # default layout before restoring
    window_state.restore_state(restored, Settings(db.connect(path)), "state")
    assert not restored_dock.isVisible()  # the closed dock stayed closed
    restored.close()
