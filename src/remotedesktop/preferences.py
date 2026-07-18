"""The Preferences tab: user-adjustable settings, persisted in `Settings`.

Currently one preference — the performance-history window length. Values live
in the shared SQLite settings table (the single-database architecture), so
they survive restarts and are injectable in tests like every other store.
"""

from collections.abc import Sequence

from PySide6.QtWidgets import QFormLayout, QSpinBox, QWidget

from remotedesktop.config import Settings
from remotedesktop.performance import PerformanceMonitor

PERFORMANCE_WINDOW_KEY = "performance_window_seconds"
DEFAULT_PERFORMANCE_WINDOW_SECONDS = 120


def load_performance_window_seconds(settings: Settings) -> int:
    raw = settings.get(PERFORMANCE_WINDOW_KEY, str(DEFAULT_PERFORMANCE_WINDOW_SECONDS))
    try:
        value = int(raw)  # ty: ignore[invalid-argument-type]
    except (TypeError, ValueError):
        return DEFAULT_PERFORMANCE_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_PERFORMANCE_WINDOW_SECONDS


class PreferencesTab(QWidget):
    def __init__(
        self,
        settings: Settings,
        monitors: PerformanceMonitor | Sequence[PerformanceMonitor],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        # The app has one monitor per role (viewing/sharing); the window
        # setting applies to all of them.
        self._monitors = (
            [monitors] if isinstance(monitors, PerformanceMonitor) else list(monitors)
        )
        self.history_minutes = QSpinBox()
        self.history_minutes.setRange(1, 30)
        self.history_minutes.setSuffix(" min")
        self.history_minutes.setValue(
            max(1, round(load_performance_window_seconds(settings) / 60))
        )
        self.history_minutes.valueChanged.connect(self._on_history_changed)
        layout = QFormLayout(self)
        layout.addRow("Performance history", self.history_minutes)

    def _on_history_changed(self, minutes: int) -> None:
        seconds = minutes * 60
        self._settings.set(PERFORMANCE_WINDOW_KEY, str(seconds))
        for monitor in self._monitors:
            monitor.set_window_seconds(float(seconds))
