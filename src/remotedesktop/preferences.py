"""The Preferences tab: user-adjustable settings.

Performance-history length and the clipboard-sync opt-out persist in
`Settings` (the shared SQLite settings table, injectable in tests like every
other store); the start-at-login option reads and writes the Windows Run
registry key via `Autostart`. The clipboard toggle applies live to the
shared `ClipboardSync` (the `clipboard=` opt-in collaborator pattern).
"""

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QSpinBox, QWidget

from remotedesktop.autostart import Autostart
from remotedesktop.config import Settings
from remotedesktop.performance import PerformanceMonitor

PERFORMANCE_WINDOW_KEY = "performance_window_seconds"
DEFAULT_PERFORMANCE_WINDOW_SECONDS = 120
CLIPBOARD_SYNC_KEY = "clipboard_sync_enabled"


def load_clipboard_sync_enabled(settings: Settings) -> bool:
    return settings.get(CLIPBOARD_SYNC_KEY, "1") != "0"


def load_performance_window_seconds(settings: Settings) -> int:
    raw = settings.get(PERFORMANCE_WINDOW_KEY, str(DEFAULT_PERFORMANCE_WINDOW_SECONDS))
    try:
        value = int(raw)  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError):
        return DEFAULT_PERFORMANCE_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_PERFORMANCE_WINDOW_SECONDS


class PreferencesTab(QWidget):
    statusMessage = Signal(str)  # for the window's Connection log pane

    def __init__(
        self,
        settings: Settings,
        monitors: PerformanceMonitor | Sequence[PerformanceMonitor],
        autostart: Autostart | None = None,
        clipboard=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._clipboard = clipboard
        # The app has one monitor per role (viewing/sharing); the window
        # setting applies to all of them.
        self._monitors = (
            [monitors] if isinstance(monitors, PerformanceMonitor) else list(monitors)
        )
        self._autostart = autostart if autostart is not None else Autostart()
        self.history_minutes = QSpinBox()
        self.history_minutes.setRange(1, 30)
        self.history_minutes.setSuffix(" min")
        self.history_minutes.setValue(
            max(1, round(load_performance_window_seconds(settings) / 60))
        )
        self.history_minutes.valueChanged.connect(self._on_history_changed)
        self.autostart_checkbox = QCheckBox("Start Remote Desktop when I log in to Windows")
        self.autostart_checkbox.setChecked(self._autostart.is_enabled())
        self.autostart_checkbox.setEnabled(self._autostart.available)
        self.autostart_checkbox.toggled.connect(self._on_autostart_toggled)
        self.clipboard_checkbox = QCheckBox("Sync clipboard with connected computers")
        self.clipboard_checkbox.setChecked(load_clipboard_sync_enabled(settings))
        self.clipboard_checkbox.toggled.connect(self._on_clipboard_toggled)
        layout = QFormLayout(self)
        layout.addRow("Performance history", self.history_minutes)
        layout.addRow(self.clipboard_checkbox)
        layout.addRow(self.autostart_checkbox)

    def _on_history_changed(self, minutes: int) -> None:
        seconds = minutes * 60
        self._settings.set(PERFORMANCE_WINDOW_KEY, str(seconds))
        for monitor in self._monitors:
            monitor.set_window_seconds(float(seconds))

    def _on_autostart_toggled(self, checked: bool) -> None:
        self._autostart.set_enabled(checked)
        self.statusMessage.emit(
            "Remote Desktop will start at login"
            if checked
            else "Remote Desktop will no longer start at login"
        )

    def _on_clipboard_toggled(self, checked: bool) -> None:
        self._settings.set(CLIPBOARD_SYNC_KEY, "1" if checked else "0")
        if self._clipboard is not None:
            self._clipboard.enabled = checked
        self.statusMessage.emit(
            "Clipboard sync enabled" if checked else "Clipboard sync disabled"
        )
