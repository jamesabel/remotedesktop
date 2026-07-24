"""Autostart tests run against an isolated registry key, never the real
Run key, so they cannot change what actually starts at login."""

import sys

import pytest

from remotedesktop.autostart import (
    START_MAXIMIZED,
    START_MINIMIZED,
    START_NORMAL,
    START_OFF,
    Autostart,
    app_command,
    installed_launcher,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")

_TEST_KEY = r"Software\remotedesktop-tests\Run"


@pytest.fixture
def autostart():
    instance = Autostart(key_path=_TEST_KEY, value_name="test-app")
    yield instance
    instance.set_mode(START_OFF)


def test_mode_round_trip(autostart):
    assert autostart.mode() == START_OFF
    for mode in (START_MINIMIZED, START_NORMAL, START_MAXIMIZED):
        autostart.set_mode(mode)
        assert autostart.mode() == mode
    autostart.set_mode(START_OFF)
    assert autostart.mode() == START_OFF


def test_off_when_not_registered_is_a_noop(autostart):
    autostart.set_mode(START_OFF)
    assert autostart.mode() == START_OFF


def test_registered_command_is_stored(autostart):
    import winreg

    autostart.set_mode(START_MINIMIZED)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _TEST_KEY) as key:
        value, kind = winreg.QueryValueEx(key, "test-app")
    assert kind == winreg.REG_SZ
    assert value == app_command(START_MINIMIZED)


def test_app_command_is_quoted_launchable_and_carries_the_mode_flag():
    for mode, suffix in (
        (START_MINIMIZED, " --minimized"),
        (START_MAXIMIZED, " --maximized"),
    ):
        command = app_command(mode)
        assert command.startswith('"')
        assert "remotedesktop" in command
        assert command.endswith(suffix)
    # A normal window is the no-flag command line.
    normal = app_command(START_NORMAL)
    assert normal == app_command(START_MINIMIZED).removesuffix(" --minimized")


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
    assert app_command(START_MINIMIZED) == f'"{launcher}" --minimized'


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
    assert app_command(START_MINIMIZED) == f'"{python}" -m remotedesktop --minimized'


def test_unavailable_autostart_is_inert(autostart):
    autostart.available = False
    autostart.set_mode(START_MINIMIZED)
    assert autostart.mode() == START_OFF
