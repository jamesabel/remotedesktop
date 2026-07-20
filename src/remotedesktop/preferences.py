"""The Preferences tab: user-adjustable settings.

Performance-history length, the clipboard-sync opt-out, and the theme choice
persist in `Settings` (the shared SQLite settings table, injectable in tests
like every other store); the start-at-login option reads and writes the
Windows Run registry key via `Autostart`. The clipboard toggle applies live
to the shared `ClipboardSync` (the `clipboard=` opt-in collaborator
pattern). The theme radios (follow-OS / light / dark) apply live via
`apply_theme`; `MainWindow` re-applies the persisted choice at startup.
"""

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
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
VIEWER_KEY = "viewer_enabled"
THEME_KEY = "theme"
THEME_SYSTEM = "system"  # follow the OS light/dark setting
THEME_LIGHT = "light"
THEME_DARK = "dark"
_THEMES = (THEME_SYSTEM, THEME_LIGHT, THEME_DARK)


def load_clipboard_sync_enabled(settings: Settings) -> bool:
    return settings.get(CLIPBOARD_SYNC_KEY, "1") != "0"


def load_theme(settings: Settings) -> str:
    value = settings.get(THEME_KEY, THEME_SYSTEM)
    return value if value in _THEMES else THEME_SYSTEM


def apply_theme(theme: str) -> None:
    """Apply a theme app-wide; Qt's windows11 style restyles live.

    THEME_SYSTEM unsets the override so the app follows the OS light/dark
    setting again (including future OS-side switches).
    """
    hints = QGuiApplication.styleHints()
    if theme == THEME_LIGHT:
        hints.setColorScheme(Qt.ColorScheme.Light)
    elif theme == THEME_DARK:
        hints.setColorScheme(Qt.ColorScheme.Dark)
    else:
        hints.unsetColorScheme()


def load_viewer_enabled(settings: Settings) -> bool:
    return settings.get(VIEWER_KEY, "1") != "0"


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
    # The viewer (client) role: whether this instance connects to other
    # computers at all. The window shows/hides the client UI accordingly.
    viewerModeChanged = Signal(bool)

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
        self.sharing_off_radio = QRadioButton(
            "Not shared — no one can see this computer's screen"
        )
        self.sharing_view_radio = QRadioButton(
            "Shared, view only — clients can watch this computer's screen "
            "but not control it"
        )
        self.sharing_control_radio = QRadioButton(
            "Shared, full control — clients can watch this computer's screen "
            "and control it with their keyboard and mouse"
        )
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
        # Theme: an explicit light/dark override, or follow the OS setting.
        self.theme_system_radio = QRadioButton("Follow the Windows light/dark setting")
        self.theme_light_radio = QRadioButton("Light")
        self.theme_dark_radio = QRadioButton("Dark")
        self._theme_radios = {
            THEME_SYSTEM: self.theme_system_radio,
            THEME_LIGHT: self.theme_light_radio,
            THEME_DARK: self.theme_dark_radio,
        }
        self._theme_radios[load_theme(settings)].setChecked(True)
        for theme, radio in self._theme_radios.items():
            radio.toggled.connect(
                lambda checked, t=theme: checked and self._on_theme_changed(t)
            )
        theme_box = QWidget()
        theme_layout = QVBoxLayout(theme_box)
        theme_layout.setContentsMargins(0, 0, 0, 0)
        for radio in self._theme_radios.values():
            theme_layout.addWidget(radio)
        self.restart_button = QPushButton("Restart app")
        self.restart_button.setToolTip(
            "Relaunch this app (e.g. after updating the software). It can be "
            "clicked from a remote desktop session, so an update doesn't "
            "require visiting this computer."
        )
        self.viewer_checkbox = QCheckBox(
            "Act as a client — discover servers on this LAN and view or "
            "control their screens"
        )
        self.viewer_checkbox.setChecked(load_viewer_enabled(settings))
        self.viewer_checkbox.toggled.connect(self._on_viewer_toggled)
        layout = QFormLayout(self)
        layout.addRow("Client (viewer)", self.viewer_checkbox)
        layout.addRow("Server (sharing)", sharing_box)
        layout.addRow("Theme", theme_box)
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

    def _on_viewer_toggled(self, checked: bool) -> None:
        self._settings.set(VIEWER_KEY, "1" if checked else "0")
        self.statusMessage.emit(
            "Client (viewer) role enabled — this computer can connect to servers"
            if checked
            else "Client (viewer) role disabled — this computer only serves"
        )
        self.viewerModeChanged.emit(checked)

    def _on_theme_changed(self, theme: str) -> None:
        self._settings.set(THEME_KEY, theme)
        apply_theme(theme)
        self.statusMessage.emit(
            {
                THEME_SYSTEM: "Theme follows the Windows light/dark setting",
                THEME_LIGHT: "Theme set to light",
                THEME_DARK: "Theme set to dark",
            }[theme]
        )

    def _on_clipboard_toggled(self, checked: bool) -> None:
        self._settings.set(CLIPBOARD_SYNC_KEY, "1" if checked else "0")
        if self._clipboard is not None:
            self._clipboard.enabled = checked
        self.statusMessage.emit(
            "Clipboard sync enabled" if checked else "Clipboard sync disabled"
        )
