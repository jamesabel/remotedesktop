"""Tests for hiding an unwanted console window.

All tests here are AI-authored. The Win32 calls are faked (the `win32`
fixture), so no test can hide or minimize the terminal running pytest.
"""

import struct
import sys
from pathlib import Path, PurePosixPath

import pytest

from remotedesktop import console_window
from remotedesktop.console_window import (
    broken_pythonw,
    hide_unwanted_console,
    is_console_program,
    wants_no_console,
)


def _pe_image(subsystem: int, pe_offset: int = 0x80) -> bytes:
    """A minimal PE header: just enough for the subsystem field to be read."""
    image = bytearray(pe_offset + 24 + 68 + 2)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<H", image, pe_offset + 24 + 68, subsystem)
    return bytes(image)


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
def test_wants_no_console_parses_windows_paths_off_windows(monkeypatch):
    # A POSIX Path doesn't split on backslashes, so its name would be the whole
    # string; the check must not depend on the host's Path flavour.
    monkeypatch.setattr(console_window, "Path", PurePosixPath)
    assert wants_no_console(r"C:\proj\.venv\Scripts\pythonw.exe") is True
    assert wants_no_console(r"C:\proj\.venv\Scripts\python.exe") is False


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


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.mark.parametrize(("subsystem", "expected"), [(3, True), (2, False)])
def test_is_console_program_reads_the_pe_subsystem(tmp_path, subsystem, expected):
    exe = tmp_path / "pythonw.exe"
    exe.write_bytes(_pe_image(subsystem))
    assert is_console_program(exe) is expected


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.mark.parametrize(
    "content",
    [
        b"",  # empty
        b"not an executable" * 10,  # no MZ
        _pe_image(3)[:0x90],  # truncated before the subsystem field
        _pe_image(3).replace(b"PE\0\0", b"XX\0\0"),  # bad PE signature
    ],
)
def test_is_console_program_is_false_for_non_pe_files(tmp_path, content):
    exe = tmp_path / "pythonw.exe"
    exe.write_bytes(content)
    assert is_console_program(exe) is False


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_is_console_program_is_false_for_a_missing_file(tmp_path):
    assert is_console_program(tmp_path / "missing.exe") is False


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.mark.skipif(sys.platform != "win32", reason="real Windows interpreters")
def test_is_console_program_on_the_real_interpreters():
    base = Path(sys.base_prefix)  # the real CPython install, not a venv shim
    assert is_console_program(base / "python.exe") is True
    assert is_console_program(base / "pythonw.exe") is False


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
@pytest.mark.parametrize(("subsystem", "broken"), [(3, True), (2, False)])
def test_broken_pythonw_checks_the_sibling_pythonw(tmp_path, subsystem, broken):
    (tmp_path / "pythonw.exe").write_bytes(_pe_image(subsystem))
    expected = tmp_path / "pythonw.exe" if broken else None
    assert broken_pythonw(str(tmp_path / "python.exe")) == expected


# AI-GENERATED TEST (Claude Code) - delete this line to make this test human-owned.
def test_broken_pythonw_is_none_without_a_pythonw(tmp_path):
    assert broken_pythonw(str(tmp_path / "python.exe")) is None
