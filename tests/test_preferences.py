import sys

import pytest

from remotedesktop import db
from remotedesktop.autostart import Autostart
from remotedesktop.config import Settings
from remotedesktop.performance import PerformanceMonitor
from remotedesktop.preferences import (
    PERFORMANCE_WINDOW_KEY,
    PreferencesTab,
    load_performance_window_seconds,
)

_TEST_AUTOSTART_KEY = r"Software\remotedesktop-tests\PreferencesRun"


def make_autostart():
    return Autostart(key_path=_TEST_AUTOSTART_KEY, value_name="prefs-test")


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
        tab.autostart_checkbox,
        tab.restart_button,
    ):
        tip = control.toolTip()
        assert tip, f"{control.text() if hasattr(control, 'text') else control} has no tooltip"
        # Long tooltips are broken into lines by hand — one endless line is
        # hard to read. Short one-liners (the theme overrides) are fine.
        assert "\n" in tip or len(tip) < 80


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_autostart_checkbox_toggles_registration(qapp, tmp_path):
    autostart = make_autostart()
    settings = Settings(db.connect(tmp_path / "prefs.db"))
    tab = PreferencesTab(settings, PerformanceMonitor(), autostart=autostart)
    messages = []
    tab.statusMessage.connect(messages.append)
    try:
        assert not autostart.is_enabled()
        tab.autostart_checkbox.setChecked(True)
        assert autostart.is_enabled()
        assert any("start at login" in m for m in messages)
        tab.autostart_checkbox.setChecked(False)
        assert not autostart.is_enabled()
    finally:
        autostart.set_enabled(False)


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
