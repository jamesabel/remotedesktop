"""Tests for hiding an unwanted console window.

All tests here are AI-authored. The Win32 calls are faked (the `win32`
fixture), so no test can hide or minimize the terminal running pytest.
"""

import pytest

from remotedesktop import console_window
from remotedesktop.console_window import hide_unwanted_console, wants_no_console


class _FakeKernel32:
    def __init__(self, hwnd):
        self.hwnd = hwnd

    def GetConsoleWindow(self):  # noqa: N802 - Win32 name
        return self.hwnd


class _FakeUser32:
    def __init__(self):
        self.calls = []

    def ShowWindow(self, hwnd, command):  # noqa: N802 - Win32 name
        self.calls.append((hwnd, command))


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.fixture
def win32(monkeypatch):
    """Fake Windows console APIs; set `.kernel32.hwnd` to simulate a console."""
    kernel32, user32 = _FakeKernel32(hwnd=1234), _FakeUser32()
    monkeypatch.setattr(console_window, "_IS_WINDOWS", True)
    monkeypatch.setattr(console_window, "_kernel32", kernel32, raising=False)
    monkeypatch.setattr(console_window, "_user32", user32, raising=False)
    return kernel32, user32


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        (r"C:\proj\.venv\Scripts\pythonw.exe", True),
        (r"C:\proj\.venv\Scripts\PYTHONW.EXE", True),
        (r"C:\proj\.venv\Scripts\python.exe", False),
        (r"C:\apps\remotedesktop_2.4.0\python.exe", False),
    ],
)
def test_wants_no_console_only_for_pythonw(executable, expected):
    assert wants_no_console(executable) is expected


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_pythonw_with_console_minimizes_then_hides(win32):
    _kernel32, user32 = win32
    assert hide_unwanted_console(r"C:\venv\Scripts\pythonw.exe") is True
    # Minimize before hide: conhost ends hidden, Windows Terminal minimized.
    assert user32.calls == [
        (1234, console_window._SW_MINIMIZE),
        (1234, console_window._SW_HIDE),
    ]


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_python_exe_console_is_left_alone(win32):
    _kernel32, user32 = win32
    assert hide_unwanted_console(r"C:\venv\Scripts\python.exe") is False
    assert user32.calls == []


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_pythonw_without_console_does_nothing(win32):
    kernel32, user32 = win32
    kernel32.hwnd = None
    assert hide_unwanted_console(r"C:\venv\Scripts\pythonw.exe") is False
    assert user32.calls == []


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_inert_off_windows(win32, monkeypatch):
    _kernel32, user32 = win32
    monkeypatch.setattr(console_window, "_IS_WINDOWS", False)
    assert hide_unwanted_console(r"C:\venv\Scripts\pythonw.exe") is False
    assert user32.calls == []
