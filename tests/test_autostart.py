"""Autostart tests run against an isolated registry key, never the real
Run key, so they cannot change what actually starts at login."""

import sys

import pytest

from remotedesktop.autostart import Autostart, server_command

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")

_TEST_KEY = r"Software\remotedesktop-tests\Run"


@pytest.fixture
def autostart():
    instance = Autostart(key_path=_TEST_KEY, value_name="test-server")
    yield instance
    instance.set_enabled(False)


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
        value, kind = winreg.QueryValueEx(key, "test-server")
    assert kind == winreg.REG_SZ
    assert value == server_command()


def test_server_command_is_quoted_and_launchable():
    command = server_command()
    assert command.startswith('"')
    assert "remotedesktop" in command


def test_unavailable_autostart_is_inert(autostart):
    autostart.available = False
    autostart.set_enabled(True)
    assert not autostart.is_enabled()
