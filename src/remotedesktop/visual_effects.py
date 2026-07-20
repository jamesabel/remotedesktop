"""Temporarily reduce Windows visual effects while viewers are connected.

Animations and fades are poison for the lossless frame pipeline: a 250 ms
minimize sweep or a menu fade is not one screen update but a burst of large
changed regions, each banded, PNG-encoded, shipped, and decoded — so the
remote UI smears where it should snap. `VisualEffectsReducer` turns the
worst offenders off while someone is actually viewing and restores the
user's exact previous values afterwards.

Everything goes through SystemParametersInfo WITHOUT SPIF_UPDATEINIFILE:
the changes are per-user, apply live (SPIF_SENDCHANGE), are never
persisted, and self-heal at logoff even if the app crashes while applied.
Only knobs that differ from the reduced value are touched, so a user who
already disabled an effect keeps their own setting untouched (and their
choice is never "restored" to something else).

On non-Windows platforms the backend reports unavailable and the reducer is
inert (the app targets Windows).
"""

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Protocol

_log = logging.getLogger("remotedesktop.visual_effects")

_IS_WINDOWS = sys.platform == "win32"

# Session-only + broadcast WM_SETTINGCHANGE so running apps re-read the
# setting. Deliberately NOT SPIF_UPDATEINIFILE: nothing is persisted.
_SPIF_SENDCHANGE = 0x02

# The reduced set, chosen for streaming cost (see the module docstring):
# (get code, set code, label). All are booleans reduced to False.
_BOOL_KNOBS = (
    # Master switch for the small effects: menu/tooltip fades and slides,
    # combo-box animation, smooth scrolling, selection fade, cursor shadow.
    (0x103E, 0x103F, "UI effects (fades and slides)"),  # SPI_GET/SETUIEFFECTS
    (0x1024, 0x1025, "window drop shadows"),  # SPI_GET/SETDROPSHADOW
    (0x1042, 0x1043, "client-area animations"),  # SPI_GET/SETCLIENTAREAANIMATION
)
_SPI_GETANIMATION = 0x0048
_SPI_SETANIMATION = 0x0049
_SPI_GETMENUSHOWDELAY = 0x006A
_SPI_SETMENUSHOWDELAY = 0x006B

# Submenus normally wait 400 ms before opening; on top of a remote round
# trip that reads as lag. Not an effect, but the single best perceived-
# latency win for remote menu navigation. Only ever lowered, never raised.
REDUCED_MENU_DELAY_MS = 50


if _IS_WINDOWS:

    class _ANIMATIONINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("iMinAnimate", ctypes.c_int)]


class EffectsBackend(Protocol):
    """What the reducer needs from a backend (tests provide a fake)."""

    available: bool

    def get_bool(self, code: int) -> bool | None: ...
    def set_bool(self, code: int, value: bool) -> None: ...
    def get_min_animate(self) -> bool | None: ...
    def set_min_animate(self, value: bool) -> None: ...
    def get_menu_delay(self) -> int | None: ...
    def set_menu_delay(self, milliseconds: int) -> None: ...


class SpiBackend:
    """The real SystemParametersInfo calls; swapped for a fake in tests."""

    def __init__(self) -> None:
        self.available = _IS_WINDOWS

    def get_bool(self, code: int) -> bool | None:
        value = wintypes.BOOL()
        if not ctypes.windll.user32.SystemParametersInfoW(code, 0, ctypes.byref(value), 0):
            return None
        return bool(value.value)

    def set_bool(self, code: int, value: bool) -> None:
        # This family of knobs takes the value cast into pvParam.
        ctypes.windll.user32.SystemParametersInfoW(
            code, 0, ctypes.c_void_p(1 if value else 0), _SPIF_SENDCHANGE
        )

    def get_min_animate(self) -> bool | None:
        """Whether windows animate on minimize/maximize."""
        info = _ANIMATIONINFO(ctypes.sizeof(_ANIMATIONINFO), 0)
        if not ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETANIMATION, ctypes.sizeof(info), ctypes.byref(info), 0
        ):
            return None
        return bool(info.iMinAnimate)

    def set_min_animate(self, value: bool) -> None:
        info = _ANIMATIONINFO(ctypes.sizeof(_ANIMATIONINFO), 1 if value else 0)
        ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETANIMATION, ctypes.sizeof(info), ctypes.byref(info), _SPIF_SENDCHANGE
        )

    def get_menu_delay(self) -> int | None:
        """The submenu-open delay in milliseconds."""
        value = wintypes.UINT()
        if not ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETMENUSHOWDELAY, 0, ctypes.byref(value), 0
        ):
            return None
        return int(value.value)

    def set_menu_delay(self, milliseconds: int) -> None:
        ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETMENUSHOWDELAY, int(milliseconds), None, _SPIF_SENDCHANGE
        )


class VisualEffectsReducer:
    """Apply the reduced effects set and restore the exact previous values.

    `apply` and `restore` are idempotent and return whether they changed
    anything, so callers can log only on the actual transitions.
    """

    def __init__(self, backend: EffectsBackend | None = None) -> None:
        self._backend = backend if backend is not None else SpiBackend()
        # None = not applied; else {knob description: saved value} to restore.
        self._saved_bools: dict[int, bool] | None = None
        self._saved_min_animate: bool | None = None
        self._saved_menu_delay: int | None = None

    @property
    def available(self) -> bool:
        return self._backend.available

    @property
    def applied(self) -> bool:
        return self._saved_bools is not None

    def apply(self) -> bool:
        if not self._backend.available or self._saved_bools is not None:
            return False
        self._saved_bools = {}
        for get_code, set_code, label in _BOOL_KNOBS:
            current = self._backend.get_bool(get_code)
            # Unreadable (skip: nothing safe to restore) or already off
            # (the user's own choice; leave it theirs).
            if not current:
                continue
            self._saved_bools[set_code] = current
            self._backend.set_bool(set_code, False)
            _log.debug("Reduced %s", label)
        min_animate = self._backend.get_min_animate()
        if min_animate:
            self._saved_min_animate = min_animate
            self._backend.set_min_animate(False)
            _log.debug("Reduced minimize/maximize animation")
        delay = self._backend.get_menu_delay()
        if delay is not None and delay > REDUCED_MENU_DELAY_MS:
            self._saved_menu_delay = delay
            self._backend.set_menu_delay(REDUCED_MENU_DELAY_MS)
            _log.debug("Reduced submenu delay %d ms -> %d ms", delay, REDUCED_MENU_DELAY_MS)
        return True

    def restore(self) -> bool:
        if self._saved_bools is None:
            return False
        for set_code, value in self._saved_bools.items():
            self._backend.set_bool(set_code, value)
        if self._saved_min_animate is not None:
            self._backend.set_min_animate(self._saved_min_animate)
        if self._saved_menu_delay is not None:
            self._backend.set_menu_delay(self._saved_menu_delay)
        self._saved_bools = None
        self._saved_min_animate = None
        self._saved_menu_delay = None
        _log.debug("Visual effects restored")
        return True
