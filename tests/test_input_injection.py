"""InputInjector unit tests.

These patch user32.SendInput with a recorder, so the real structure-building
code runs but nothing ever reaches the OS — the host's mouse and keyboard
must never move during tests.
"""

import ctypes
import sys

import pytest

from remotedesktop import input_injection as ii
from remotedesktop.input_injection import InputInjector

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only API")


@pytest.fixture
def sent(monkeypatch):
    """Recorded INPUT structures instead of real injection."""
    records = []

    def fake_send_input(count, pointer, size):
        records.append(pointer._obj)
        return count

    monkeypatch.setattr(ctypes.windll.user32, "SendInput", fake_send_input)
    return records


def test_move_maps_normalized_to_absolute(sent):
    InputInjector().move(0.5, 0.25)
    (record,) = sent
    assert record.type == ii._INPUT_MOUSE
    assert record.u.mi.dx == round(0.5 * 65535)
    assert record.u.mi.dy == round(0.25 * 65535)
    assert record.u.mi.dwFlags & ii._MOUSEEVENTF_MOVE
    assert record.u.mi.dwFlags & ii._MOUSEEVENTF_ABSOLUTE


def test_move_clamps_out_of_range_coordinates(sent):
    InputInjector().move(2.0, -1.0)
    (record,) = sent
    assert record.u.mi.dx == 65535
    assert record.u.mi.dy == 0


def test_button_press_and_positionless_release(sent):
    injector = InputInjector()
    injector.button(0.5, 0.5, "left", True)
    injector.button(None, None, "left", False)
    press, release = sent
    assert press.u.mi.dwFlags & ii._MOUSE_BUTTON_FLAGS["left"][0]
    assert press.u.mi.dwFlags & ii._MOUSEEVENTF_MOVE
    assert release.u.mi.dwFlags & ii._MOUSE_BUTTON_FLAGS["left"][1]
    # No coordinates -> released at the current cursor position, no move.
    assert not release.u.mi.dwFlags & ii._MOUSEEVENTF_MOVE


def test_unknown_button_is_ignored(sent):
    InputInjector().button(0.5, 0.5, "back", True)
    assert sent == []


def test_wheel_delta_is_encoded_as_dword(sent):
    InputInjector().wheel(0.5, 0.5, -120)
    (record,) = sent
    assert record.u.mi.dwFlags & ii._MOUSEEVENTF_WHEEL
    assert record.u.mi.mouseData == (-120) & 0xFFFFFFFF


def test_key_down_and_up(sent):
    injector = InputInjector()
    injector.key(65, True)
    injector.key(65, False)
    down, up = sent
    assert down.type == ii._INPUT_KEYBOARD
    assert down.u.ki.wVk == 65
    assert not down.u.ki.dwFlags & ii._KEYEVENTF_KEYUP
    assert up.u.ki.dwFlags & ii._KEYEVENTF_KEYUP
    assert down.u.ki.wScan != 0  # scan code filled in for apps that read it


def test_extended_keys_carry_the_extended_flag(sent):
    InputInjector().key(0x25, True)  # VK_LEFT
    (record,) = sent
    assert record.u.ki.dwFlags & ii._KEYEVENTF_EXTENDEDKEY


def test_zero_vk_is_ignored(sent):
    InputInjector().key(0, True)
    assert sent == []


def test_unavailable_injector_does_nothing(sent):
    injector = InputInjector()
    injector.available = False
    injector.move(0.5, 0.5)
    injector.button(0.5, 0.5, "left", True)
    injector.wheel(0.5, 0.5, 120)
    injector.key(65, True)
    assert sent == []
