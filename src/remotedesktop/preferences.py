"""The Preferences tab: user-adjustable settings.

Performance-history length, the clipboard-sync opt-out, and the theme choice
persist in `Settings` (the shared SQLite settings table, injectable in tests
like every other store); the start-at-login choice (a four-way start mode —
minimized is the default and recommended) persists there too
(`autostart_mode`) and is mirrored into the Windows Run registry key via
`Autostart` — at every construction, so the default takes effect on a fresh
install without anyone visiting Preferences. The clipboard toggle applies live
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

from remotedesktop.autostart import (
    START_MAXIMIZED,
    START_MINIMIZED,
    START_MODES,
    START_NORMAL,
    START_OFF,
    Autostart,
)
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
REDUCE_EFFECTS_KEY = "reduce_effects_enabled"
VIEWER_KEY = "viewer_enabled"
AUTOSTART_MODE_KEY = "autostart_mode"
THEME_KEY = "theme"
THEME_SYSTEM = "system"  # follow the OS light/dark setting
THEME_LIGHT = "light"
THEME_DARK = "dark"
_THEMES = (THEME_SYSTEM, THEME_LIGHT, THEME_DARK)


def load_clipboard_sync_enabled(settings: Settings) -> bool:
    return settings.get_bool(CLIPBOARD_SYNC_KEY, True)


def load_reduce_effects_enabled(settings: Settings) -> bool:
    # Default on (per the user's request): the effects come back the moment
    # the last viewer disconnects, and nothing is persisted OS-side.
    return settings.get_bool(REDUCE_EFFECTS_KEY, True)


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
    return settings.get_bool(VIEWER_KEY, True)


def load_autostart_mode(settings: Settings) -> str:
    """The persisted start-at-login mode; minimized is the default."""
    value = settings.get(AUTOSTART_MODE_KEY, START_MINIMIZED)
    return value if value in START_MODES else START_MINIMIZED


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
    # Reduce Windows visual effects while viewers are connected; the window
    # owns the VisualEffectsReducer and applies/restores on this.
    reduceEffectsChanged = Signal(bool)

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
        self.history_minutes.setToolTip(
            "How much recent history the Performance tab's bandwidth and\n"
            "round-trip-time graphs (and their statistics) cover."
        )
        self.history_minutes.setRange(1, 30)
        self.history_minutes.setSuffix(" min")
        self.history_minutes.setValue(
            max(1, round(load_performance_window_seconds(settings) / 60))
        )
        self.history_minutes.valueChanged.connect(self._on_history_changed)
        # How (and whether) the app starts at login: a four-way start mode.
        self.autostart_minimized_radio = QRadioButton("Start minimized (recommended)")
        self.autostart_minimized_radio.setToolTip(
            "Registers this app in your Windows startup (per-user, no admin\n"
            "rights) so it launches at login minimized — straight to the\n"
            "tray while sharing is on — so sharing resumes after a reboot\n"
            "without anyone having to launch the app."
        )
        self.autostart_normal_radio = QRadioButton("Start with a normal window")
        self.autostart_normal_radio.setToolTip(
            "Launches at login (per-user Windows startup, no admin rights)\n"
            "with a normal window."
        )
        self.autostart_maximized_radio = QRadioButton("Start maximized")
        self.autostart_maximized_radio.setToolTip(
            "Launches at login (per-user Windows startup, no admin rights)\n"
            "with a maximized window."
        )
        self.autostart_off_radio = QRadioButton("Do not start Remote Desktop")
        self.autostart_off_radio.setToolTip(
            "No startup registration: Remote Desktop only runs when you\n"
            "launch it yourself."
        )
        self._autostart_radios = {
            START_MINIMIZED: self.autostart_minimized_radio,
            START_NORMAL: self.autostart_normal_radio,
            START_MAXIMIZED: self.autostart_maximized_radio,
            START_OFF: self.autostart_off_radio,
        }
        autostart_mode = load_autostart_mode(settings)
        self._autostart_radios[autostart_mode].setChecked(True)
        # Mirror the persisted choice into the Run key on every start, so the
        # start-minimized default takes effect on a fresh install (and the
        # registered path follows the current installation).
        self._autostart.set_mode(autostart_mode)
        for mode, radio in self._autostart_radios.items():
            radio.setEnabled(self._autostart.available)
            radio.toggled.connect(
                lambda checked, m=mode: checked and self._on_autostart_mode_changed(m)
            )
        autostart_box = QWidget()
        autostart_layout = QVBoxLayout(autostart_box)
        autostart_layout.setContentsMargins(0, 0, 0, 0)
        for radio in self._autostart_radios.values():
            autostart_layout.addWidget(radio)
        self.clipboard_checkbox = QCheckBox("Sync clipboard with connected computers")
        self.clipboard_checkbox.setToolTip(
            "Text, images, and files copied on this computer appear on\n"
            "connected computers, and theirs appear here (files up to 32 MB\n"
            "per copy; folders are not synced).\n"
            "Turn off to keep this computer's clipboard private — nothing\n"
            "is sent or applied in either direction."
        )
        self.clipboard_checkbox.setChecked(load_clipboard_sync_enabled(settings))
        self.clipboard_checkbox.toggled.connect(self._on_clipboard_toggled)
        self.reduce_effects_checkbox = QCheckBox(
            "Reduce Windows visual effects while sharing this screen (recommended)"
        )
        self.reduce_effects_checkbox.setToolTip(
            "Makes the remote view feel more responsive: window animations,\n"
            "menu fades, and shadows each stream as a burst of screen\n"
            "updates, so turning them off while viewers are connected lets\n"
            "the remote screen snap instead of smearing — and saves\n"
            "bandwidth. Submenus also open faster.\n"
            "Your Windows settings come back when the last viewer\n"
            "disconnects; nothing is permanently changed."
        )
        self.reduce_effects_checkbox.setChecked(load_reduce_effects_enabled(settings))
        self.reduce_effects_checkbox.toggled.connect(self._on_reduce_effects_toggled)
        # The three-state sharing choice: off, view-only, or full control.
        self.sharing_off_radio = QRadioButton(
            "Not shared — no one can see this computer's screen"
        )
        self.sharing_off_radio.setToolTip(
            "This computer neither listens for connections nor announces\n"
            "itself on the LAN. Existing viewers are disconnected."
        )
        self.sharing_view_radio = QRadioButton(
            "Shared, view only — clients can watch this computer's screen "
            "but not control it"
        )
        self.sharing_view_radio.setToolTip(
            "Approved clients see this screen live, but their keyboard and\n"
            "mouse input is ignored.\n"
            "Switching between view only and full control applies instantly\n"
            "without disconnecting viewers."
        )
        self.sharing_control_radio = QRadioButton(
            "Shared, full control — clients can watch this computer's screen "
            "and control it with their keyboard and mouse"
        )
        self.sharing_control_radio.setToolTip(
            "Approved clients see this screen live and can type and click\n"
            "as if sitting at this computer.\n"
            "Each new client needs a one-time approval on this computer\n"
            "before it can connect."
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
        self.theme_system_radio.setToolTip(
            "Match Windows: the app switches automatically when the\n"
            "Windows light/dark mode changes."
        )
        self.theme_light_radio = QRadioButton("Light")
        self.theme_light_radio.setToolTip("Always use the light theme.")
        self.theme_dark_radio = QRadioButton("Dark")
        self.theme_dark_radio.setToolTip("Always use the dark theme.")
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
            "Relaunch this app (e.g. after updating the software).\n"
            "It can be clicked from a remote desktop session, so an update\n"
            "doesn't require visiting this computer."
        )
        self.viewer_checkbox = QCheckBox(
            "Act as a client — discover servers on this LAN and view or "
            "control their screens"
        )
        self.viewer_checkbox.setToolTip(
            "Shows the client-side UI: the server panel, session tabs, and\n"
            "server history.\n"
            "Turn off on a computer that only shares its screen — it will\n"
            "not connect to (or scan for) other computers at all."
        )
        self.viewer_checkbox.setChecked(load_viewer_enabled(settings))
        self.viewer_checkbox.toggled.connect(self._on_viewer_toggled)
        layout = QFormLayout(self)
        layout.addRow("Client (viewer)", self.viewer_checkbox)
        layout.addRow("Server (sharing)", sharing_box)
        layout.addRow("Theme", theme_box)
        layout.addRow("Performance history", self.history_minutes)
        layout.addRow(self.clipboard_checkbox)
        layout.addRow(self.reduce_effects_checkbox)
        layout.addRow("When I log in to Windows", autostart_box)
        layout.addRow(self.restart_button)

    def _on_history_changed(self, minutes: int) -> None:
        seconds = minutes * 60
        self._settings.set(PERFORMANCE_WINDOW_KEY, str(seconds))
        for monitor in self._monitors:
            monitor.set_window_seconds(float(seconds))

    def _on_autostart_mode_changed(self, mode: str) -> None:
        self._settings.set(AUTOSTART_MODE_KEY, mode)
        self._autostart.set_mode(mode)
        self.statusMessage.emit(
            {
                START_MINIMIZED: "Remote Desktop will start minimized at login",
                START_NORMAL: "Remote Desktop will start at login",
                START_MAXIMIZED: "Remote Desktop will start maximized at login",
                START_OFF: "Remote Desktop will no longer start at login",
            }[mode]
        )

    def _on_viewer_toggled(self, checked: bool) -> None:
        self._settings.set_bool(VIEWER_KEY, checked)
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

    def _on_reduce_effects_toggled(self, checked: bool) -> None:
        self._settings.set_bool(REDUCE_EFFECTS_KEY, checked)
        self.statusMessage.emit(
            "Windows visual effects will be reduced while viewers are connected"
            if checked
            else "Windows visual effects will be left unchanged while sharing"
        )
        self.reduceEffectsChanged.emit(checked)

    def _on_clipboard_toggled(self, checked: bool) -> None:
        self._settings.set_bool(CLIPBOARD_SYNC_KEY, checked)
        if self._clipboard is not None:
            self._clipboard.enabled = checked
        self.statusMessage.emit(
            "Clipboard sync enabled" if checked else "Clipboard sync disabled"
        )
