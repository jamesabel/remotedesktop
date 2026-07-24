import sys

import pytest

from remotedesktop import db
from remotedesktop.autostart import (
    START_MAXIMIZED,
    START_MINIMIZED,
    START_OFF,
    Autostart,
)
from remotedesktop.config import Settings
from remotedesktop.performance import PerformanceMonitor
from remotedesktop.preferences import (
    AUTOSTART_MODE_KEY,
    PERFORMANCE_WINDOW_KEY,
    PreferencesTab,
    load_autostart_mode,
    load_performance_window_seconds,
)

_TEST_AUTOSTART_KEY = r"Software\remotedesktop-tests\PreferencesRun"


def make_autostart():
    return Autostart(key_path=_TEST_AUTOSTART_KEY, value_name="prefs-test")


@pytest.fixture(autouse=True)
def _clean_test_run_key():
    # Constructing a PreferencesTab mirrors the persisted start mode into the
    # (injected, isolated) Run key — leave nothing behind.
    yield
    make_autostart().set_mode(START_OFF)


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
    tab = PreferencesTab(settings, monitor, autostart=make_autostart())
    assert tab.history_minutes.value() == 2  # the 2-minute default
    tab.history_minutes.setValue(5)  # fires valueChanged
    assert settings.get(PERFORMANCE_WINDOW_KEY) == "300"
    assert monitor.window_seconds == 300.0
    # A fresh tab (new window / restart) reads the persisted value.
    tab2 = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
    assert tab2.history_minutes.value() == 5


def test_history_change_applies_to_all_monitors(qapp, tmp_path):
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    monitors = [PerformanceMonitor(), PerformanceMonitor()]
    tab = PreferencesTab(settings, monitors, autostart=make_autostart())
    tab.history_minutes.setValue(3)
    assert all(m.window_seconds == 180.0 for m in monitors)


def test_every_preference_control_has_a_multiline_tooltip(qapp, tmp_path):
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    tab = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
    assert tab.reduce_effects_checkbox.text().endswith("(recommended)")
    for control in (
        tab.viewer_checkbox,
        tab.sharing_off_radio,
        tab.sharing_view_radio,
        tab.sharing_control_radio,
        tab.theme_system_radio,
        tab.theme_light_radio,
        tab.theme_dark_radio,
        tab.history_minutes,
        tab.clipboard_checkbox,
        tab.reduce_effects_checkbox,
        tab.autostart_minimized_radio,
        tab.autostart_normal_radio,
        tab.autostart_maximized_radio,
        tab.autostart_off_radio,
        tab.restart_button,
    ):
        tip = control.toolTip()
        assert tip, f"{control.text() if hasattr(control, 'text') else control} has no tooltip"
        # Long tooltips are broken into lines by hand — one endless line is
        # hard to read. Short one-liners (the theme overrides) are fine.
        assert "\n" in tip or len(tip) < 80


def test_autostart_mode_defaults_and_invalid_fall_back(tmp_path):
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    assert load_autostart_mode(settings) == START_MINIMIZED  # the recommended default
    settings.set(AUTOSTART_MODE_KEY, "sideways")
    assert load_autostart_mode(settings) == START_MINIMIZED


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_autostart_mode_radios_register_persist_and_apply(qapp, tmp_path):
    autostart = make_autostart()
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    tab = PreferencesTab(settings, PerformanceMonitor(), autostart=autostart)
    messages = []
    tab.statusMessage.connect(messages.append)
    # The default (start minimized) is selected AND registered at
    # construction, so a fresh install starts at login with no clicks.
    assert tab.autostart_minimized_radio.isChecked()
    assert autostart.mode() == START_MINIMIZED
    tab.autostart_maximized_radio.setChecked(True)
    assert autostart.mode() == START_MAXIMIZED
    assert settings.get(AUTOSTART_MODE_KEY) == START_MAXIMIZED
    assert any("start maximized at login" in m for m in messages)
    tab.autostart_off_radio.setChecked(True)
    assert autostart.mode() == START_OFF
    assert any("no longer start at login" in m for m in messages)
    # A fresh tab (restart) reads the persisted choice and keeps it applied.
    tab2 = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
    assert tab2.autostart_off_radio.isChecked()
    assert autostart.mode() == START_OFF


def test_clipboard_toggle_persists_and_applies_live(qapp, tmp_path):
    from remotedesktop.preferences import CLIPBOARD_SYNC_KEY, load_clipboard_sync_enabled

    class RecordingSync:
        enabled = True

    settings = Settings(db.connect(tmp_path / "prefs.db"))
    clipboard = RecordingSync()
    tab = PreferencesTab(
        settings, PerformanceMonitor(), autostart=make_autostart(), clipboard=clipboard
    )
    messages = []
    tab.statusMessage.connect(messages.append)
    assert tab.clipboard_checkbox.isChecked()  # default on
    tab.clipboard_checkbox.setChecked(False)
    assert settings.get(CLIPBOARD_SYNC_KEY) == "0"
    assert clipboard.enabled is False
    assert any("disabled" in m for m in messages)
    # A fresh tab (restart) reads the persisted off state.
    tab2 = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
    assert not tab2.clipboard_checkbox.isChecked()
    assert not load_clipboard_sync_enabled(settings)


def test_theme_defaults_and_invalid_fall_back(tmp_path):
    from remotedesktop.preferences import THEME_KEY, THEME_SYSTEM, load_theme

    settings = Settings(db.connect(tmp_path / "prefs.db"))
    assert load_theme(settings) == THEME_SYSTEM
    settings.set(THEME_KEY, "sepia")
    assert load_theme(settings) == THEME_SYSTEM


def test_theme_radio_persists_and_applies_live(qapp, tmp_path):
    from PySide6.QtCore import Qt

    from remotedesktop.preferences import THEME_KEY, THEME_SYSTEM, apply_theme

    settings = Settings(db.connect(tmp_path / "prefs.db"))
    tab = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
    assert tab.theme_system_radio.isChecked()  # default: follow the OS
    messages = []
    tab.statusMessage.connect(messages.append)
    try:
        tab.theme_dark_radio.setChecked(True)
        assert settings.get(THEME_KEY) == "dark"
        assert qapp.styleHints().colorScheme() == Qt.ColorScheme.Dark
        assert any("dark" in m for m in messages)
        # A fresh tab (restart) reads the persisted choice.
        tab2 = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
        assert tab2.theme_dark_radio.isChecked()
    finally:
        apply_theme(THEME_SYSTEM)  # don't leak a dark palette into other tests


def test_sharing_mode_radios_reflect_persisted_state_and_emit(qapp, tmp_path):
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    settings.set("server_enabled", "1")
    settings.set("allow_remote_input", "0")
    tab = PreferencesTab(settings, PerformanceMonitor(), autostart=make_autostart())
    assert tab.sharing_view_radio.isChecked()  # persisted: shared, view only
    modes = []
    tab.sharingModeChanged.connect(modes.append)
    tab.sharing_control_radio.setChecked(True)
    assert modes == ["control"]
    tab.sharing_off_radio.setChecked(True)
    assert modes == ["control", "off"]
