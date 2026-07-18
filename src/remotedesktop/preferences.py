"""The Preferences tab: user-adjustable settings.

Performance-history length and the clipboard-sync opt-out persist in
`Settings` (the shared SQLite settings table, injectable in tests like every
other store); the start-at-login option reads and writes the Windows Run
registry key via `Autostart`. The clipboard toggle applies live to the
shared `ClipboardSync` (the `clipboard=` opt-in collaborator pattern).
"""

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from remotedesktop.autostart import Autostart
from remotedesktop.config import Settings
from remotedesktop.performance import PerformanceMonitor
from remotedesktop.server import (
    SHARING_MODE_CONTROL,
    SHARING_MODE_OFF,
    SHARING_MODE_VIEW,
    load_sharing_mode,
)

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
    # The three-state sharing mode ("off"/"view"/"control"); the window wires
    # this to SharingTab.set_mode, which owns persistence and the lifecycle.
    sharingModeChanged = Signal(str)

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
        # The three-state sharing choice: off, view-only, or full control.
        self.sharing_off_radio = QRadioButton("Not shared")
        self.sharing_view_radio = QRadioButton("Shared — viewers can watch only")
        self.sharing_control_radio = QRadioButton("Shared — viewers can watch and control")
        self._mode_radios = {
            SHARING_MODE_OFF: self.sharing_off_radio,
            SHARING_MODE_VIEW: self.sharing_view_radio,
            SHARING_MODE_CONTROL: self.sharing_control_radio,
        }
        self._mode_radios[load_sharing_mode(settings)].setChecked(True)
        for mode, radio in self._mode_radios.items():
            radio.toggled.connect(
                lambda checked, m=mode: checked and self.sharingModeChanged.emit(m)
            )
        sharing_box = QWidget()
        sharing_layout = QVBoxLayout(sharing_box)
        sharing_layout.setContentsMargins(0, 0, 0, 0)
        for radio in self._mode_radios.values():
            sharing_layout.addWidget(radio)
        self.restart_button = QPushButton("Restart app")
        self.restart_button.setToolTip(
            "Relaunch this app (e.g. after updating the software). It can be "
            "clicked from a remote desktop session, so an update doesn't "
            "require visiting this computer."
        )
        layout = QFormLayout(self)
        layout.addRow("Screen sharing", sharing_box)
        layout.addRow("Performance history", self.history_minutes)
        layout.addRow(self.clipboard_checkbox)
        layout.addRow(self.autostart_checkbox)
        layout.addRow(self.restart_button)

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
