"""Autostart tests run against an isolated registry key, never the real
Run key, so they cannot change what actually starts at login."""

import sys

import pytest

from remotedesktop.autostart import Autostart, app_command, installed_launcher

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")

_TEST_KEY = r"Software\remotedesktop-tests\Run"


@pytest.fixture
def autostart():
    instance = Autostart(
        key_path=_TEST_KEY, value_name="test-app", legacy_value_name="test-legacy"
    )
    yield instance
    instance.set_enabled(False)
    instance._delete_value("test-legacy")


def test_enable_disable_round_trip(autostart):
    assert not autostart.is_enabled()
    autostart.set_enabled(True)
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_disable_when_not_registered_is_a_noop(autostart):
    autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_registered_command_is_stored(autostart):
    import winreg

    autostart.set_enabled(True)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TEST_KEY) as key:
        value, kind = winreg.QueryValueEx(key, "test-app")
    assert kind == winreg.REG_SZ
    assert value == app_command()


def test_app_command_is_quoted_launchable_and_minimized():
    command = app_command()
    assert command.startswith('"')
    assert "remotedesktop" in command
    # Login-started instances go straight to the tray when sharing.
    assert command.endswith("--minimized")


def test_app_command_prefers_installed_launcher(tmp_path, monkeypatch):
    # A pyship install: sys.executable inside a versioned CLIP dir, the
    # launcher exe (which always starts the newest CLIP) one level up.
    clip_python = tmp_path / "remotedesktop_1.2.3" / "pythonw.exe"
    clip_python.parent.mkdir()
    clip_python.touch()
    launcher = tmp_path / "remotedesktop" / "remotedesktop.exe"
    launcher.parent.mkdir()
    launcher.touch()
    monkeypatch.setattr(sys, "executable", str(clip_python))
    assert installed_launcher() == launcher
    assert app_command() == f'"{launcher}" --minimized'


def test_non_clip_layout_is_not_mistaken_for_an_install(tmp_path, monkeypatch):
    # Same sibling layout but the interpreter dir isn't a versioned CLIP dir —
    # must fall back rather than register a look-alike exe.
    python = tmp_path / "Scripts" / "pythonw.exe"
    python.parent.mkdir()
    python.touch()
    lookalike = tmp_path / "remotedesktop" / "remotedesktop.exe"
    lookalike.parent.mkdir()
    lookalike.touch()
    monkeypatch.setattr(sys, "executable", str(python))
    assert installed_launcher() is None
    assert app_command() == f'"{python}" -m remotedesktop --minimized'


def test_legacy_server_registration_migrates(autostart):
    import winreg

    # Simulate a pre-1.0 install that had the server-only app registered.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _TEST_KEY) as key:
        winreg.SetValueEx(key, "test-legacy", 0, winreg.REG_SZ, '"old-server.exe"')
    autostart.migrate_legacy()
    assert autostart.is_enabled()  # re-registered under the new name
    assert not autostart._has_value("test-legacy")  # old value removed


def test_migrate_without_legacy_value_changes_nothing(autostart):
    autostart.migrate_legacy()
    assert not autostart.is_enabled()


def test_enabling_clears_a_lingering_legacy_value(autostart):
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _TEST_KEY) as key:
        winreg.SetValueEx(key, "test-legacy", 0, winreg.REG_SZ, '"old-server.exe"')
    autostart.set_enabled(True)
    assert autostart.is_enabled()
    assert not autostart._has_value("test-legacy")


def test_unavailable_autostart_is_inert(autostart):
    autostart.available = False
    autostart.set_enabled(True)
    assert not autostart.is_enabled()
