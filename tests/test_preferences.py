from remotedesktop import db
from remotedesktop.config import Settings
from remotedesktop.performance import PerformanceMonitor
from remotedesktop.preferences import (
    PERFORMANCE_WINDOW_KEY,
    PreferencesTab,
    load_performance_window_seconds,
)


def test_default_and_invalid_values_fall_back(tmp_path):
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    assert load_performance_window_seconds(settings) == 120
    settings.set(PERFORMANCE_WINDOW_KEY, "garbage")
    assert load_performance_window_seconds(settings) == 120
    settings.set(PERFORMANCE_WINDOW_KEY, "-5")
    assert load_performance_window_seconds(settings) == 120


def test_history_change_persists_and_applies(qapp, tmp_path):
    connection = db.connect(tmp_path / "prefs.db")
    settings = Settings(connection)
    monitor = PerformanceMonitor()
    tab = PreferencesTab(settings, monitor)
    assert tab.history_minutes.value() == 2  # the 2-minute default
    tab.history_minutes.setValue(5)  # fires valueChanged
    assert settings.get(PERFORMANCE_WINDOW_KEY) == "300"
    assert monitor.window_seconds == 300.0
    # A fresh tab (new window / restart) reads the persisted value.
    tab2 = PreferencesTab(settings, PerformanceMonitor())
    assert tab2.history_minutes.value() == 5
