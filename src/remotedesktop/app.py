"""The Remote Desktop app: view other computers, and optionally share this
one — both roles in a single window.

Viewing: the Servers dock discovers servers on the LAN; each connection is a
`ServerSession` shown in its own closable tab (`remotedesktop.client`).
The "Connections" tab groups everything connection-related — sharing status
and viewers (`remotedesktop.server.SharingTab`), both peer inventories, and
the connection log — while the sharing mode itself (off / view only / full
control) is chosen in Preferences. While sharing is enabled, closing the
window hides to the system tray and sharing continues; quitting is in the
tray menu. Only one instance runs per user session (`single_instance`)."""

import json
import logging
import sqlite3
import sys
import time

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import __version__, compat, db, icon, logs, window_state
from remotedesktop.about import AboutTab
from remotedesktop.autostart import Autostart, installed_launcher
from remotedesktop.client import DiscoveryPanel, ServerSession
from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import KnownServers, Settings, default_db_path, load_client_identity
from remotedesktop.discovery import DEFAULT_CONNECT_PORT, DISCOVERY_PORT, ServerInfo
from remotedesktop.inventory import ConnectionInventory, InventoryTab
from remotedesktop.logs import PeerLogDialog, read_log_tail
from remotedesktop.modal_loop import HTCLOSE, HTMAXBUTTON, HTMINBUTTON, ModalLoopPump
from remotedesktop.performance import PerformanceMonitor, PerformanceTab
from remotedesktop.preferences import (
    PreferencesTab,
    apply_theme,
    load_clipboard_sync_enabled,
    load_performance_window_seconds,
    load_reduce_effects_enabled,
    load_theme,
    load_viewer_enabled,
)
from remotedesktop.server import SharingTab, ViewersTable
from remotedesktop.sharing import ShareClient
from remotedesktop.single_instance import SingleInstance
from remotedesktop.viewer import ViewerWidget
from remotedesktop.visual_effects import VisualEffectsReducer

_log = logging.getLogger("remotedesktop.app")

# Auto-reconnect backoff never waits longer than this between attempts.
_RECONNECT_CAP_SECONDS = 30.0


class _SessionsTable(ViewersTable):
    """The client-side twin of the viewers table: one row per connected
    server session, with the same live network statistics."""

    _COLUMNS = [
        "Name", "Address", "Version", "Send", "Receive",
        "RTT", "RTT mean", "RTT min", "RTT max", "RTT p99", "RTT jitter",
    ]
    _RATE_COLUMNS = (3, 4)
    _MS_COLUMNS = (5, 6, 7, 8, 9, 10)
    _METRIC_COLUMNS = _RATE_COLUMNS + _MS_COLUMNS

    def _identity_values(self, viewer: dict) -> list[str]:
        return [
            viewer["name"] or "(unknown)",
            viewer["address"],
            self._version_cell(viewer["app_version"]),
        ]


class _ConnectedServersSource(QObject):
    """Adapts the window's connected sessions to the ViewersTable source
    protocol (a `viewers()` method plus a change signal)."""

    clientCountChanged = Signal(int)

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self._window = window

    def viewers(self) -> list[dict]:
        return [
            {
                "name": session.name,
                "address": session.key,
                "app_version": session.client.server_app_version,
                "stream": session.client.stream,
            }
            for session in self._window._sessions
            if session.connected
        ]

    def notify(self) -> None:
        self.clientCountChanged.emit(len(self.viewers()))


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        connection: sqlite3.Connection | None = None,
        auto_scan: bool = True,
        credentials=None,
        autostart: Autostart | None = None,
        discovery_port: int = DISCOVERY_PORT,
        connect_port: int = DEFAULT_CONNECT_PORT,
        tray_available: bool | None = None,
        reconnect_base_seconds: float = 2.0,
        effects_reducer: VisualEffectsReducer | None = None,
    ) -> None:
        super().__init__()
        self._reconnect_base = reconnect_base_seconds
        self._auto_scan = auto_scan
        self.setWindowIcon(icon.app_icon("app"))
        # Tests inject a connection to a temp database; the app uses the default.
        self._db = connection if connection is not None else db.connect(default_db_path())
        self._settings = Settings(self._db)
        # The viewer (client) role is opt-out: a dedicated server turns it
        # off and loses the client-side UI (Server tab, discovery, history).
        self._viewer_enabled = load_viewer_enabled(self._settings)
        # Restore the persisted theme before any widgets are built, so the
        # window never flashes in the wrong scheme.
        apply_theme(load_theme(self._settings))
        # One monitor per role: ShareServer.close() resets its monitor, so
        # toggling sharing must not share a monitor with the viewing sessions
        # (and vice versa for _connect_session's reset).
        window_seconds = float(load_performance_window_seconds(self._settings))
        self.client_performance = PerformanceMonitor(window_seconds=window_seconds, parent=self)
        self.server_performance = PerformanceMonitor(window_seconds=window_seconds, parent=self)
        self.client_inventory = ConnectionInventory(self._db, "client_peers", self)
        self.server_inventory = ConnectionInventory(self._db, "server_peers", self)
        # One OS clipboard, one sync, shared by both roles: a real local copy
        # fans out to connected servers and own viewers; a payload received
        # from a peer is applied with its signature recorded first, so it
        # never re-emits `changed` and cannot loop between the roles. The
        # persisted Preferences opt-out applies from the first moment.
        self._clipboard = ClipboardSync(parent=self)
        self._clipboard.enabled = load_clipboard_sync_enabled(self._settings)
        self._known_servers = KnownServers(self._db)
        self._identity = load_client_identity(self._db)
        self._tray_available = (
            QSystemTrayIcon.isSystemTrayAvailable() if tray_available is None else tray_available
        )
        self._tray: QSystemTrayIcon | None = None
        self._tray_notified = False
        self._quitting = False
        # Windows visual effects are reduced only while someone is actually
        # watching (fewer animation frames to encode and ship) and restored
        # when the last viewer leaves. The default reducer changes real OS
        # settings — tests always inject one with a fake backend.
        self._effects_reducer = (
            effects_reducer if effects_reducer is not None else VisualEffectsReducer()
        )
        self._reduce_effects = load_reduce_effects_enabled(self._settings)
        self._sharing_viewer_count = 0
        self._fullscreen_state: dict | None = None
        self._fullscreen_hint: QLabel | None = None

        self.sharing_tab = SharingTab(
            settings=self._settings,
            connection=self._db,
            performance=self.server_performance,
            clipboard=self._clipboard,
            credentials=credentials,
            discovery_port=discovery_port,
            connect_port=connect_port,
        )
        self.sharing_tab.statusMessage.connect(self.log)
        self.sharing_tab.peerEvent.connect(self._record_server_peer)
        self.sharing_tab.sharingChanged.connect(self._on_sharing_changed)
        self.sharing_tab.viewerCountChanged.connect(self._update_sharing_indicator)
        self.sharing_tab.viewerCountChanged.connect(self._on_viewer_count_changed)

        # One tab per server connection (inserted at the front, closable),
        # followed by the fixed tabs, which never get a close button. While
        # no session exists, a placeholder "Server" tab holds instructions;
        # the first connection takes its place (and its position), and it
        # returns when the last session closes.
        self._sessions: list[ServerSession] = []
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.currentChanged.connect(self._on_current_tab_changed)
        self._no_session_page = self._build_no_session_page()
        if self._viewer_enabled:
            self._tabs.addTab(self._no_session_page, "Server")
        # One "Connections" tab holds everything connection-related in a 2×2
        # grid: sharing status + viewers, both peer inventories, and the
        # connection log.
        self.connection_log = QPlainTextEdit(self)
        self.connection_log.setReadOnly(True)
        self.connection_log.setMaximumBlockCount(1000)
        self.get_log_button = QPushButton("Get server log")
        self.get_log_button.setToolTip(
            "Ask the server shown in the current tab to send its debug log"
        )
        self.get_log_button.clicked.connect(self._request_server_log)
        self.get_client_log_button = QPushButton("Get client log")
        self.get_client_log_button.setToolTip(
            "Ask the most recently connected viewer of this computer to send its debug log"
        )
        self.get_client_log_button.clicked.connect(self.sharing_tab.request_client_log)

        connections_tab = QWidget()
        # One outer group box per role, side by side as WIDGETS (not column
        # layouts): a hidden widget frees its space, so when a role is off
        # the other box starts at the left edge instead of leaving a gap.
        # Client box: live connected-server sessions (with the same network
        # statistics as the viewers table) over the persisted server
        # records. Server box: live sharing status/viewers over the
        # persisted client pairings.
        self._client_role_group = QGroupBox("Client (viewer)")
        client_role_layout = QVBoxLayout(self._client_role_group)
        self._sessions_source = _ConnectedServersSource(self)
        self.sessions_table = _SessionsTable(self.client_performance)
        self.sessions_table.set_share_server(self._sessions_source)
        sessions_box = QGroupBox("Connected servers")
        QVBoxLayout(sessions_box).addWidget(self.sessions_table)
        # "History" (not "on LAN") — the live discovery list is the left
        # panel; these tables are the persisted first/last-seen records.
        servers_history_box = QGroupBox("Server history")
        QVBoxLayout(servers_history_box).addWidget(
            InventoryTab(self.client_inventory, "Forget", self._forget_server)
        )
        client_role_layout.addWidget(sessions_box)
        client_role_layout.addWidget(servers_history_box)

        self._server_role_group = QGroupBox("Server (sharing)")
        server_role_layout = QVBoxLayout(self._server_role_group)
        server_role_layout.addWidget(self.sharing_tab)
        clients_history_box = QGroupBox("Client history")
        QVBoxLayout(clients_history_box).addWidget(
            InventoryTab(self.server_inventory, "Revoke", self._revoke_client)
        )
        server_role_layout.addWidget(clients_history_box)

        log_group = QGroupBox("Connection log")
        log_layout = QVBoxLayout(log_group)
        log_buttons = QHBoxLayout()
        log_buttons.addWidget(self.get_log_button)
        log_buttons.addWidget(self.get_client_log_button)
        log_buttons.addStretch(1)
        log_layout.addLayout(log_buttons)
        log_layout.addWidget(self.connection_log)
        # Client box left, Server box right (the order the indicators and
        # Preferences use); the log spans the bottom.
        columns = QHBoxLayout()
        columns.addWidget(self._client_role_group, 1)
        columns.addWidget(self._server_role_group, 1)
        connections_layout = QVBoxLayout(connections_tab)
        connections_layout.addLayout(columns, 2)
        connections_layout.addWidget(log_group, 1)
        self._update_connections_groups()
        self._tabs.addTab(connections_tab, "Connections")
        # The Performance sub-tabs follow the roles: "Viewing" exists only
        # with the viewer role, "Sharing" only while actually serving —
        # a role that produces no data gets no sub-tab. The pages are kept
        # as attributes so _update_performance_tabs can re-insert them.
        self.performance_pages = QTabWidget()
        self._viewing_perf_page = PerformanceTab(
            self.client_performance, local="client", remote="server"
        )
        self._sharing_perf_page = PerformanceTab(
            self.server_performance, local="server", remote="client"
        )
        self._performance_hint = QLabel(
            "Nothing to measure — enable the Client (viewer) or "
            "Server (sharing) role in the Preferences tab"
        )
        self._performance_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._performance_hint.hide()
        performance_tab = QWidget()
        performance_layout = QVBoxLayout(performance_tab)
        performance_layout.addWidget(self.performance_pages, 1)
        performance_layout.addWidget(
            self._performance_hint, 1, Qt.AlignmentFlag.AlignCenter
        )
        self._tabs.addTab(performance_tab, "Performance")
        self.setCentralWidget(self._tabs)

        # auto_scan=False (tests) never broadcasts on the LAN; the app scans
        # once at startup — and not at all without the viewer role.
        self.discovery_panel = DiscoveryPanel(
            self,
            is_self=self._is_own_server,
            auto_scan=auto_scan and self._viewer_enabled,
        )
        # The dock holds the role indicators (always) and the discovery
        # panel (viewer role only). The indicators are button-shaped for
        # visibility but purely informational: mouse events pass straight
        # through, and the roles are changed only in Preferences.
        self.client_role_button = self._make_role_button()
        self.server_role_button = self._make_role_button()
        dock_body = QWidget()
        self._dock_layout = QVBoxLayout(dock_body)
        self._dock_layout.addWidget(self.client_role_button)
        self._dock_layout.addWidget(self.server_role_button)
        self._dock_layout.addWidget(self.discovery_panel)
        # A permanent trailing spacer: it takes the leftover height while
        # the discovery panel is hidden (keeping the indicators pinned to
        # the top), and collapses to nothing while the panel is visible
        # (the stretch factors swap in _update_dock_layout).
        self._dock_layout.addStretch(0)
        if not self._viewer_enabled:
            self.discovery_panel.hide()
        self._update_dock_layout()
        # No title bar at all: the contents are self-explanatory, and the
        # View menu ("Panel") is the way to show or hide it — a blank header
        # strip whose only job was the X earned no space. The Closable
        # feature must stay even though there is no X: without it Qt
        # disables the toggleViewAction, killing the View-menu toggle.
        self.panel_dock = QDockWidget("", self)
        # An object name is required for saveState() to persist the dock.
        self.panel_dock.setObjectName("panel_dock")
        self.panel_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.panel_dock.setTitleBarWidget(QWidget(self.panel_dock))
        self.panel_dock.setWidget(dock_body)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.panel_dock)

        self.preferences_tab = PreferencesTab(
            self._settings,
            [self.client_performance, self.server_performance],
            autostart=autostart,
            clipboard=self._clipboard,
        )
        self.preferences_tab.statusMessage.connect(self.log)
        # Preferences drives both roles: the sharing lifecycle (the
        # three-state choice) and the viewer role's UI.
        self.preferences_tab.sharingModeChanged.connect(self.sharing_tab.set_mode)
        self.preferences_tab.viewerModeChanged.connect(self._set_viewer_enabled)
        self.preferences_tab.reduceEffectsChanged.connect(self._on_reduce_effects_changed)
        self.preferences_tab.restart_button.clicked.connect(self._restart_app)
        self._tabs.addTab(self.preferences_tab, "Preferences")
        self._about_tab = AboutTab()
        self._tabs.addTab(self._about_tab, "About")
        # Only session tabs are closable; strip the buttons the fixed tabs
        # got from setTabsClosable (styles place them on either side).
        bar = self._tabs.tabBar()
        for index in range(self._tabs.count()):
            for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
                bar.setTabButton(index, side, None)

        self.discovery_panel.serverActivated.connect(self._on_server_activated)
        self.discovery_panel.serversFound.connect(self._record_discovered)
        self.discovery_panel.status.connect(self.log)

        # While this window sits in Windows' modal move/size loop (title-bar
        # drag — including one driven by an injected remote click while
        # sharing), Qt stops running; the pump keeps sockets and timers
        # serviced so a remote mouse-up can still arrive and end the drag
        # instead of deadlocking. Caption-button presses are handled by the
        # pump directly — their native tracking loop cannot be pumped.
        self._modal_pump = ModalLoopPump(caption_action=self._on_caption_button)

        self._build_menus()
        self._update_window_title()
        # Permanent right-side indicator: sharing state at a glance whatever
        # tab is current (the message area follows the selected session).
        self._sharing_indicator = QLabel()
        self._sharing_indicator.hide()
        self.statusBar().addPermanentWidget(self._sharing_indicator)
        self.statusBar().showMessage("Not connected")
        window_state.restore_geometry(self, self._settings, window_state.MAIN_GEOMETRY_KEY)
        window_state.restore_state(self, self._settings, window_state.MAIN_STATE_KEY)
        self.log("Remote Desktop started")
        # Now that the log pane, tray state, and signal wiring exist, start
        # sharing if the persisted opt-in is on, and reopen the connections
        # that were open when the app last ran.
        self.sharing_tab.restore_sharing()
        self._restore_sessions()
        self._update_role_indicators()
        self._update_performance_tabs()

    # ------------------------------------------------------------- logging

    def log(self, message: str) -> None:
        # Everything shown in the Connection log pane also goes to the debug
        # log file (when main() enabled it), so it survives the window.
        _log.info(message)
        self.connection_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    # ------------------------------------------------------- window chrome

    def _build_menus(self) -> None:
        # Menus and actions are kept as attributes: PySide6 gives the Python
        # wrapper ownership of objects returned by addMenu/addAction, so a
        # garbage-collected local would delete the underlying C++ object.
        bar = self.menuBar()
        self._file_menu = bar.addMenu("&File")
        self.restart_action = self._file_menu.addAction("&Restart app…")
        self.restart_action.triggered.connect(self._restart_app)
        self.close_tab_action = self._file_menu.addAction("&Close tab")
        self.close_tab_action.setShortcut(QKeySequence("Ctrl+W"))
        self.close_tab_action.triggered.connect(self._close_current_session_tab)
        self.close_tab_action.setEnabled(False)  # startup tab is a fixed tab
        self._file_menu.addSeparator()
        self.quit_action = self._file_menu.addAction("&Quit")
        self.quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self.quit_action.triggered.connect(self._quit)
        self._view_menu = bar.addMenu("&View")
        # The dock's own toggle action: the way back after closing the panel.
        self.panel_action = self.panel_dock.toggleViewAction()
        self.panel_action.setText("&Panel")
        self._view_menu.addAction(self.panel_action)
        self.refresh_action = self._view_menu.addAction("&Refresh server list")
        self.refresh_action.setShortcut(QKeySequence("F5"))
        self.refresh_action.triggered.connect(self.discovery_panel.refresh)
        self.refresh_action.setEnabled(self._viewer_enabled)
        self._view_menu.addSeparator()
        self.actual_size_action = self._view_menu.addAction("&Actual size")
        self.actual_size_action.setCheckable(True)
        self.actual_size_action.setEnabled(False)  # session tabs only
        # `triggered` (not `toggled`): tab-switch sync via setChecked must
        # not re-apply the mode.
        self.actual_size_action.triggered.connect(self._on_actual_size_triggered)
        self.fullscreen_action = self._view_menu.addAction("&Full screen")
        self.fullscreen_action.setShortcut(QKeySequence("F11"))
        self.fullscreen_action.setCheckable(True)
        self.fullscreen_action.setEnabled(False)  # session tabs only
        self.fullscreen_action.triggered.connect(self._toggle_fullscreen)
        self._help_menu = bar.addMenu("&Help")
        self.about_action = self._help_menu.addAction("&About")
        self.about_action.triggered.connect(
            lambda: self._tabs.setCurrentWidget(self._about_tab)
        )
        # Register shortcut actions on the window itself so they keep firing
        # when the menu bar is hidden (fullscreen). While a viewer has a
        # frame and focus, its ShortcutOverride handling forwards these keys
        # to the remote machine instead — F11 is the one reserved local key.
        for action in (
            self.close_tab_action,
            self.quit_action,
            self.refresh_action,
            self.fullscreen_action,
        ):
            self.addAction(action)

    @staticmethod
    def _build_no_session_page() -> QWidget:
        page = QWidget()
        label = QLabel(
            "<h3>No server connected</h3>"
            "<p>Computers sharing their screen appear automatically in the "
            "panel on the left<br>"
            "(View&nbsp;▸&nbsp;Panel if it is hidden).</p>"
            "<p>Double-click one — or select it and click <b>Connect</b> — "
            "and this tab becomes your view of that computer.</p>"
            "<p>To make a computer appear in the list, enable "
            "<b>Server (sharing)</b> in the Preferences tab on that computer.</p>"
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout = QVBoxLayout(page)
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)
        return page

    def _strip_tab_buttons(self, index: int) -> None:
        bar = self._tabs.tabBar()
        for side in (QTabBar.ButtonPosition.LeftSide, QTabBar.ButtonPosition.RightSide):
            bar.setTabButton(index, side, None)

    def _ensure_placeholder(self) -> None:
        """Re-show the "Server" instructions tab once no session remains."""
        if (
            not self._viewer_enabled
            or self._sessions
            or self._tabs.indexOf(self._no_session_page) != -1
        ):
            return
        self._tabs.insertTab(0, self._no_session_page, "Server")
        self._strip_tab_buttons(0)  # a fresh insert grows new close buttons

    @staticmethod
    def _make_role_button() -> QPushButton:
        button = QPushButton()
        button.setCheckable(True)
        button.setStyleSheet(
            "QPushButton { padding: 4px 8px; }"
            "QPushButton:checked { background-color: #2e7d32; color: white; "
            "border: 1px solid #1b5e20; border-radius: 3px; }"
        )
        # Indicator only: clicks fall through, nothing toggles from here.
        button.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _update_connections_groups(self) -> None:
        """Show each Connections-tab role box only while its role is on —
        a client-only instance has no viewers or pairings to display, and a
        server-only one has no sessions or server history."""
        self._client_role_group.setVisible(self._viewer_enabled)
        self._server_role_group.setVisible(self.sharing_tab.serving)

    def _update_performance_tabs(self) -> None:
        """Show a Performance sub-tab per role that can produce data."""
        pages = self.performance_pages

        def sync(page: QWidget, title: str, wanted: bool, front: bool) -> None:
            index = pages.indexOf(page)
            if wanted and index == -1:
                pages.insertTab(0 if front else pages.count(), page, title)
            elif not wanted and index != -1:
                pages.removeTab(index)

        sync(self._viewing_perf_page, "Client (viewer)", self._viewer_enabled, front=True)
        sync(self._sharing_perf_page, "Server (sharing)", self.sharing_tab.serving, front=False)
        empty = pages.count() == 0
        pages.setVisible(not empty)
        self._performance_hint.setVisible(empty)

    def _update_dock_layout(self) -> None:
        """Give the dock's leftover height to the panel or the spacer."""
        panel_visible = self._viewer_enabled
        self._dock_layout.setStretch(2, 1 if panel_visible else 0)  # panel
        self._dock_layout.setStretch(3, 0 if panel_visible else 1)  # spacer

    def _update_role_indicators(self) -> None:
        self.client_role_button.setChecked(self._viewer_enabled)
        self.client_role_button.setText(
            "Client (viewer): " + ("on" if self._viewer_enabled else "off")
        )
        serving = self.sharing_tab.serving
        self.server_role_button.setChecked(serving)
        self.server_role_button.setText(
            "Server (sharing): " + ("on" if serving else "off")
        )

    def _set_viewer_enabled(self, enabled: bool) -> None:
        """Show or hide the client-side UI as the viewer role toggles."""
        self._viewer_enabled = enabled
        if not enabled:
            for session in list(self._sessions):
                self._close_session(session)  # drops any open connections
            placeholder_index = self._tabs.indexOf(self._no_session_page)
            if placeholder_index != -1:
                self._tabs.removeTab(placeholder_index)
            self.discovery_panel.hide()
        else:
            self.discovery_panel.show()
            self._ensure_placeholder()
            self._tabs.setCurrentIndex(0)
            if self._auto_scan:
                self.discovery_panel.refresh()  # becoming a viewer: scan now
        self.refresh_action.setEnabled(enabled)
        self._update_dock_layout()
        self._update_role_indicators()
        self._update_performance_tabs()
        self._update_connections_groups()

    def _on_current_tab_changed(self, _index: int) -> None:
        self._refresh_status_bar()
        if getattr(self, "close_tab_action", None) is None:
            return  # menus are built after the tabs
        session = self._session_for_page(self._tabs.currentWidget())
        self.close_tab_action.setEnabled(session is not None)
        self.fullscreen_action.setEnabled(session is not None)
        self.actual_size_action.setEnabled(session is not None)
        self.actual_size_action.setChecked(session.actual_size if session else False)

    def _close_current_session_tab(self) -> None:
        session = self._session_for_page(self._tabs.currentWidget())
        if session is not None:
            self._close_session(session)

    def _on_actual_size_triggered(self, checked: bool) -> None:
        session = self._session_for_page(self._tabs.currentWidget())
        if session is None:
            return
        session.actual_size = checked
        session.page.setWidgetResizable(not checked)
        session.viewer.set_actual_size(checked)

    # --------------------------------------------------------- fullscreen

    def _toggle_fullscreen(self) -> None:
        if self._fullscreen_state is not None:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self) -> None:
        if self._session_for_page(self._tabs.currentWidget()) is None:
            self.fullscreen_action.setChecked(False)
            return
        self._fullscreen_state = {
            "maximized": self.isMaximized(),
            "dock_visible": self.panel_dock.isVisible(),
        }
        self.menuBar().hide()
        self.statusBar().hide()
        self.panel_dock.hide()
        self._tabs.tabBar().hide()
        self.showFullScreen()
        self.fullscreen_action.setChecked(True)
        self._show_fullscreen_hint()

    def _exit_fullscreen(self) -> None:
        state = self._fullscreen_state
        if state is None:
            return
        self._fullscreen_state = None
        if self._fullscreen_hint is not None:
            self._fullscreen_hint.deleteLater()
            self._fullscreen_hint = None
        self.menuBar().show()
        self.statusBar().show()
        # A dock the user had closed before fullscreen stays closed.
        self.panel_dock.setVisible(state["dock_visible"])
        self._tabs.tabBar().show()
        if state["maximized"]:
            self.showMaximized()
        else:
            self.showNormal()
        self.fullscreen_action.setChecked(False)

    def _show_fullscreen_hint(self) -> None:
        hint = QLabel("F11 exits full screen", self)
        hint.setStyleSheet(
            "background-color: rgba(0, 0, 0, 180); color: white; "
            "padding: 6px 14px; border-radius: 4px;"
        )
        self._fullscreen_hint = hint

        def place() -> None:
            # Deferred: the fullscreen resize hasn't happened yet when the
            # hint is created. The stored reference is cleared on exit, so a
            # dangling wrapper here means fullscreen already ended.
            if self._fullscreen_hint is not hint:
                return
            hint.adjustSize()
            hint.move((self.width() - hint.width()) // 2, 32)
            hint.show()
            hint.raise_()

        QTimer.singleShot(250, place)
        QTimer.singleShot(3500, hint.hide)

    def _update_window_title(self) -> None:
        """Connected server names lead the title, so the taskbar (and a
        minimized window) says who this instance is viewing; a suffix marks
        an instance that is sharing its own screen."""
        names = [session.name for session in self._sessions if session.connected]
        base = f"Remote Desktop {__version__}"
        if self.sharing_tab.serving:
            base += " — sharing"
        title = f"{', '.join(names)} — {base}" if names else base
        self.setWindowTitle(title)
        if self._tray is not None:
            self._tray.setToolTip(title)

    def bring_to_front(self) -> None:
        """Show and focus the window (e.g. a second launch yielded to us)."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ---------------------------------------------------------------- tray

    def _ensure_tray(self) -> None:
        if self._tray is not None or not self._tray_available:
            return
        tray = QSystemTrayIcon(icon.app_icon("app"), self)
        menu = QMenu(self)
        menu.addAction("Show window", self.bring_to_front)
        menu.addAction("Restart app", self._restart_app)
        menu.addSeparator()
        menu.addAction("Quit", self._quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.bring_to_front()

    def _update_sharing_indicator(self, count: int) -> None:
        if not self.sharing_tab.serving:
            self._sharing_indicator.hide()
            return
        self._sharing_indicator.setText(
            f"Sharing — {count} viewer(s)" if count else "Sharing — no viewers"
        )
        self._sharing_indicator.show()

    def _on_sharing_changed(self, serving: bool) -> None:
        if serving:
            self._ensure_tray()
        elif self._tray is not None:
            # No reason for a tray icon while not sharing; if the window was
            # hidden in the tray, surface it first so the app stays reachable.
            if self.isHidden():
                self.bring_to_front()
            self._tray.hide()
            self._tray.deleteLater()
            self._tray = None
        self._update_window_title()
        self._update_role_indicators()
        self._update_performance_tabs()
        self._update_connections_groups()

    def _on_viewer_count_changed(self, count: int) -> None:
        self._sharing_viewer_count = count
        self._update_effects_reduction()

    def _on_reduce_effects_changed(self, enabled: bool) -> None:
        self._reduce_effects = enabled
        self._update_effects_reduction()  # applies/restores live mid-session

    def _update_effects_reduction(self) -> None:
        """Reduce Windows visual effects while the preference is on AND a
        viewer is connected; restore the user's values otherwise. Stop,
        quit, and restart all reach here via their viewerCountChanged(0)."""
        if self._reduce_effects and self._sharing_viewer_count > 0:
            if self._effects_reducer.apply():
                self.log(
                    "Windows visual effects reduced while viewers are connected "
                    "(restored when the last one disconnects)"
                )
        elif self._effects_reducer.restore():
            self.log("Windows visual effects restored")

    def _quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._fullscreen_state is not None:
            # Restore the chrome first so the persisted layout never captures
            # the stripped fullscreen state.
            self._exit_fullscreen()
        window_state.save_geometry(self, self._settings, window_state.MAIN_GEOMETRY_KEY)
        window_state.save_state(self, self._settings, window_state.MAIN_STATE_KEY)
        if self.sharing_tab.serving and self._tray is not None and not self._quitting:
            # Sharing continues in the background; the tray icon is the way
            # back in (or out).
            event.ignore()
            self.hide()
            if not self._tray_notified:
                self._tray_notified = True
                self._tray.showMessage(
                    "Remote Desktop",
                    "Still sharing this computer's screen in the background. "
                    "Use the tray icon to reopen or quit.",
                )
            return
        # Really quitting: with viewers connected, stopping their stream
        # deserves a confirmation (restart has its own, and shuts sharing
        # down first, so it never double-prompts here).
        viewer_count = self.sharing_tab.viewer_count
        if self.sharing_tab.serving and viewer_count > 0:
            if self.isHidden():
                self.bring_to_front()  # the question must be visible
            answer = QMessageBox.question(
                self,
                "Quit",
                f"{viewer_count} viewer(s) are connected to this computer — "
                "quit and stop sharing?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._quitting = False
                event.ignore()
                return
        for session in self._sessions:
            self._cancel_reconnect(session)
            session.client.close()
        self.sharing_tab.shutdown()
        # shutdown's viewerCountChanged(0) already restored the effects;
        # this is the safety net for any exit path that skipped the signal.
        self._effects_reducer.restore()
        super().closeEvent(event)
        QApplication.quit()  # main() disables quit-on-last-window-closed

    # -------------------------------------------------------- app restart

    def _restart_app(self) -> None:
        """Relaunch this app in a new process and exit.

        Meant to be clicked through a remote desktop session after updating
        the software, so the new version starts without anyone at this
        computer. The listening sockets are closed before spawning so the
        replacement can bind the same ports.
        """
        if self.isHidden():
            self.bring_to_front()  # the confirmation must be visible
        answer = QMessageBox.question(
            self,
            "Restart app",
            "Restart the Remote Desktop app?\n\n"
            "All connections drop — viewers of this computer and your open "
            "sessions — and can reconnect in a few seconds (approved peers "
            "reconnect without a new permission prompt).",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # An installed (pyship) instance relaunches via the launcher exe, which
        # picks the newest installed version — so restart-after-update actually
        # starts the update. From source/venv, relaunch this interpreter.
        launcher = installed_launcher()
        if launcher is not None:
            program, args = str(launcher), []
        else:
            program, args = sys.executable, ["-m", "remotedesktop"]
        self.log("Restarting: freeing ports and launching a new process")
        _log.info("Restart requested — relaunching %s %s", program, " ".join(args))
        self.sharing_tab.shutdown()
        if not QProcess.startDetached(program, args):
            # Extremely unlikely (the program path exists); the app stays open —
            # sharing is stopped, but the machine isn't left with nothing.
            self.log("Restart failed: could not launch a new process — restart manually")
            _log.error("QProcess.startDetached failed for %s", program)
            return
        self._quit()

    # ------------------------------------------------- native modal loops

    def _on_caption_button(self, hit_code: int) -> None:
        # Deferred: the action (especially close) must not run inside the
        # native message handler that reported the press.
        if hit_code == HTMINBUTTON:
            action = self.showMinimized
        elif hit_code == HTMAXBUTTON:
            action = self.showNormal if self.isMaximized() else self.showMaximized
        elif hit_code == HTCLOSE:
            action = self.close
        else:
            return
        QTimer.singleShot(0, action)

    def nativeEvent(self, event_type, message):
        if self._modal_pump.handle_native_event(event_type, message):
            return True, 0
        return super().nativeEvent(event_type, message)

    # ------------------------------------------------------- serving role

    def _record_server_peer(self, event: dict) -> None:
        if event["event"] == "revoked":
            # Symmetric with forgetting a server: a revoked client's row is
            # deleted from the table and the DB rather than lingering. A new
            # connection attempt from it records the peer afresh.
            self.server_inventory.remove(event["key"])
            return
        self.server_inventory.record(
            event["key"],
            event["event"],
            name=event.get("name", ""),
            address=event.get("address", ""),
            detail=event.get("detail", ""),
        )

    def _revoke_client(self, client_id: str) -> None:
        answer = QMessageBox.question(
            self,
            "Revoke access",
            f"Revoke access for client {client_id}?\n\n"
            "It will be disconnected now and must be approved again to reconnect.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.sharing_tab.revoke_client(client_id)

    def _is_own_server(self, server: ServerInfo) -> bool:
        """Is a discovered server this very instance's own ShareServer?"""
        share_server = self.sharing_tab.share_server
        if share_server is None or server.port != share_server.port:
            return False
        local = {"127.0.0.1", "::1"} | {
            address.toString() for address in QNetworkInterface.allAddresses()
        }
        return server.host in local

    # ------------------------------------------------------- viewing role

    def _persist_sessions(self) -> None:
        """Remember the open session tabs so a restart can reopen them."""
        entries = [
            {"host": s.host, "port": s.port, "name": s.name}
            for s in self._sessions
            if s.port
        ]
        self._settings.set("open_sessions", json.dumps(entries))

    def _restore_sessions(self) -> None:
        if not self._viewer_enabled:
            return
        raw = self._settings.get("open_sessions")
        if not raw:
            return
        try:
            entries = json.loads(raw)
        except ValueError:
            return
        for entry in entries if isinstance(entries, list) else []:
            host, port = entry.get("host"), entry.get("port")
            if not isinstance(host, str) or not isinstance(port, int):
                continue
            key = f"{host}:{port}"
            if self._session_for_key(key) is not None:
                continue
            session = self._create_session(key, str(entry.get("name") or key))
            self.log(f"Restoring connection to {session.name} ({key})")
            self._connect_session(session, host, port)
            # A restored connection keeps trying if the server isn't up yet:
            # arm the backoff loop that normally arms only on success.
            session.auto_reconnect = True

    def _session_for_key(self, key: str) -> ServerSession | None:
        for session in self._sessions:
            if session.key == key:
                return session
        return None

    def _session_for_page(self, widget) -> ServerSession | None:
        for session in self._sessions:
            if session.page is widget:
                return session
        return None

    def _set_session_status(self, session: ServerSession, text: str) -> None:
        session.status_text = text
        if self._tabs.currentWidget() is session.page:
            self.statusBar().showMessage(text)

    def _refresh_status_bar(self) -> None:
        session = self._session_for_page(self._tabs.currentWidget())
        if session is not None:
            self.statusBar().showMessage(session.status_text)
            return
        names = [s.name for s in self._sessions if s.connected]
        self.statusBar().showMessage(
            f"Connected to {', '.join(names)}" if names else "Not connected"
        )

    def _forget_server(self, key: str) -> None:
        answer = QMessageBox.question(
            self,
            "Forget server",
            f"Forget server {key}?\n\n"
            "If connected it will be disconnected, and the next connection will "
            "need the server user to approve this computer again.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        session = self._session_for_key(key)
        if session is not None:
            self._close_session(session)
        self._known_servers.forget(key)
        # Forgetting removes the server entirely — from the table and the DB —
        # rather than leaving a "forgotten" row behind. A rescan that still
        # finds it on the LAN records it afresh as "discovered".
        self.client_inventory.remove(key)
        self.log(f"Forgot server {key}")

    def _record_discovered(self, servers: list) -> None:
        for server in servers:
            key = f"{server.host}:{server.port}"
            session = self._session_for_key(key)
            if session is not None and session.connected:
                continue  # don't downgrade a connected server to "discovered"
            self.client_inventory.record(
                key, "discovered", name=server.name, address=key, detail=key
            )

    def _on_server_activated(self, server: ServerInfo) -> None:
        if not self._viewer_enabled:
            self.log("Viewer role is off — enable it in Preferences to connect")
            return
        if self._is_own_server(server):
            # Viewing your own screen through yourself is a hall of mirrors;
            # the panel already blocks this, but guard the entry point too.
            self.log("This computer cannot connect to itself")
            return
        key = f"{server.host}:{server.port}"
        session = self._session_for_key(key)
        if session is not None and session.connected:
            self._tabs.setCurrentWidget(session.page)
            self.log(f"Already connected to {session.name} ({key})")
            return
        if session is None:
            session = self._create_session(key, server.name)
        else:
            session.name = server.name
            self._tabs.setTabText(self._sessions.index(session), session.name)
        self._connect_session(session, server.host, server.port)

    def _create_session(self, key: str, name: str) -> ServerSession:
        viewer = ViewerWidget()
        # The tab page is a scroll area: fit mode (widgetResizable) sizes the
        # viewer to the viewport as before; actual-size mode sizes the viewer
        # to the frame and the scroll area provides the panning.
        page = QScrollArea()
        page.setWidget(viewer)
        page.setWidgetResizable(True)
        page.setFrameShape(QFrame.Shape.NoFrame)
        page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        page.viewport().setStyleSheet("background-color: black;")
        client = ShareClient(
            identity=self._identity,
            known_servers=self._known_servers,
            clipboard=self._clipboard,
            performance=self.client_performance,
            log_provider=lambda: read_log_tail("remotedesktop"),
            parent=self,
        )
        session = ServerSession(key, name, client, viewer, page)
        viewer.inputEvent.connect(lambda event, s=session: self._on_input_event(s, event))
        client.status.connect(lambda message, s=session: self.log(f"[{s.name}] {message}"))
        client.connected.connect(lambda server_name, s=session: self._on_connected(s, server_name))
        client.approvalPending.connect(lambda s=session: self._on_approval_pending(s))
        client.denied.connect(lambda reason, s=session: self._on_denied(s, reason))
        client.disconnected.connect(lambda s=session: self._on_disconnected(s))
        client.frameReceived.connect(lambda image, s=session: self._on_frame(s, image))
        client.cursorShapeChanged.connect(lambda shape, s=session: s.viewer.set_remote_cursor(shape))
        client.logReceived.connect(lambda text, s=session: self._show_server_log(s.name, text))
        client.connectionFailed.connect(lambda _reason, s=session: self._schedule_reconnect(s))
        self._sessions.append(session)
        placeholder_index = self._tabs.indexOf(self._no_session_page)
        if placeholder_index != -1:
            # The first session takes the placeholder's place and position.
            self._tabs.removeTab(placeholder_index)
        self._tabs.insertTab(len(self._sessions) - 1, page, session.name)
        return session

    def _connect_session(self, session: ServerSession, host: str, port: int) -> None:
        session.host, session.port = host, port
        # A manual activation resets auto-reconnect: it re-arms only once
        # this attempt actually connects.
        self._cancel_reconnect(session)
        session.auto_reconnect = False
        session.frame_count = 0
        session.connected = False
        session.denied = False
        session.version_mismatch = False
        self.client_inventory.record(
            session.key, "attempt", name=session.name,
            address=session.key, detail=session.key,
        )
        # Graphs show only the current connection(s): clear them for a fresh
        # start, but never while another session's stream is being sampled.
        if not any(s.connected for s in self._sessions if s is not session):
            self.client_performance.reset()
        session.viewer.clear(f"Connecting to {session.name} …")
        self._tabs.setCurrentWidget(session.page)
        self._set_session_status(
            session, f"Connecting to {session.name} ({session.key}) …"
        )
        self._persist_sessions()
        session.client.connect_to(host, port)

    def _close_session(self, session: ServerSession) -> None:
        """Disconnect the session and remove its tab (tab close / forget)."""
        self._cancel_reconnect(session)
        session.auto_reconnect = False
        session.client.close()  # a synchronous disconnect signal may fire here
        index = self._sessions.index(session)
        was_current = self._tabs.currentWidget() is session.page
        self._sessions.pop(index)
        self._tabs.removeTab(index)
        self._persist_sessions()  # a closed tab is not reopened on restart
        self._ensure_placeholder()
        if was_current and not self._sessions:
            self._tabs.setCurrentIndex(0)  # back to the instructions tab
        if session.connected and not session.denied:
            self.client_inventory.record(session.key, "disconnected", name=session.name)
        session.connected = False
        session.client.deleteLater()
        session.page.deleteLater()  # the viewer is its child
        self.log(f"Closed connection to {session.name} ({session.key})")
        self._update_window_title()
        self._refresh_status_bar()
        self._sessions_source.notify()

    def _on_tab_close_requested(self, index: int) -> None:
        if index < len(self._sessions):  # fixed tabs carry no close button
            self._close_session(self._sessions[index])

    def _on_approval_pending(self, session: ServerSession) -> None:
        if session not in self._sessions:
            return
        session.viewer.clear(
            f"Waiting for approval — someone at {session.name} "
            "must allow this connection"
        )
        self._set_session_status(
            session,
            f"Waiting for the user on {session.name} to approve this computer …",
        )

    def _server_label(self, session: ServerSession) -> str:
        """The server's name with its app version when it reported one,
        e.g. 'DEN-PC (1.0.0)' — flagged when its major version differs."""
        version = session.client.server_app_version
        if not version:
            return session.name
        marker = " ⚠ VERSION MISMATCH" if session.version_mismatch else ""
        return f"{session.name} ({version}{marker})"

    def _on_connected(self, session: ServerSession, server_name: str) -> None:
        if session not in self._sessions:
            return
        session.name = server_name or session.name
        self._tabs.setTabText(self._sessions.index(session), session.name)
        session.connected = True
        # A live connection arms auto-reconnect and resets its backoff.
        self._cancel_reconnect(session)
        session.auto_reconnect = True
        # Semver policy: matching majors are the compatibility contract. A
        # mismatch warns loudly (log, dialog, status bar) but never blocks —
        # the user may still try, with no guarantees.
        warning = compat.mismatch_warning(
            __version__, session.client.server_app_version, "server"
        )
        session.version_mismatch = warning is not None
        if warning:
            self.log(warning)
            box = QMessageBox(
                QMessageBox.Icon.Warning,
                "Version mismatch",
                warning,
                QMessageBox.StandardButton.Ok,
                self,
            )
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            box.show()  # non-modal: streaming continues behind it
        self.client_inventory.record(session.key, "connected", name=session.name)
        self._persist_sessions()  # the welcome may have renamed the session
        self._sessions_source.notify()
        session.viewer.setFocus()
        self._set_session_status(
            session,
            f"Connected to {self._server_label(session)} — waiting for first frame "
            "(click the view to control it)",
        )
        self._update_window_title()

    def _on_input_event(self, session: ServerSession, event: dict) -> None:
        if session.connected:
            session.client.send_input(event)

    def _on_denied(self, session: ServerSession, reason: str) -> None:
        if session not in self._sessions:
            return
        session.connected = False
        session.denied = True
        # A denial is authoritative: no automatic retry until the user
        # activates the server again.
        self._cancel_reconnect(session)
        session.auto_reconnect = False
        self.client_inventory.record(session.key, "denied", name=session.name)
        session.viewer.clear(f"Connection denied: {reason}")
        self._set_session_status(session, f"Denied by {session.name}: {reason}")
        self._update_window_title()

    def _on_disconnected(self, session: ServerSession) -> None:
        if session not in self._sessions:
            return
        session.connected = False
        # After a denial, keep "denied" as the peer's state in the inventory
        # rather than overwriting it with the trailing "disconnected".
        if not session.denied:
            self.client_inventory.record(session.key, "disconnected", name=session.name)
            session.viewer.clear("Disconnected")
            self._set_session_status(session, f"Disconnected from {session.name}")
        self._update_window_title()
        self._sessions_source.notify()
        if session.auto_reconnect and not session.denied and not self._quitting:
            self._schedule_reconnect(session)

    def _schedule_reconnect(self, session: ServerSession) -> None:
        """Queue an auto-reconnect attempt with exponential backoff.

        The active-timer guard also dedupes the errorOccurred+disconnected
        double-fire a remote-host-closed produces.
        """
        if (
            session not in self._sessions
            or not session.auto_reconnect
            or session.denied
            or session.connected
            or self._quitting
            or (session.reconnect_timer is not None and session.reconnect_timer.isActive())
        ):
            return
        session.reconnect_attempts += 1
        delay = min(
            _RECONNECT_CAP_SECONDS,
            self._reconnect_base * 2 ** (session.reconnect_attempts - 1),
        )
        timer = session.reconnect_timer
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda s=session: self._attempt_reconnect(s))
            session.reconnect_timer = timer
        timer.start(round(delay * 1000))
        message = (
            f"Connection lost — reconnecting in {delay:.0f} s … "
            f"(attempt {session.reconnect_attempts})"
        )
        session.viewer.clear(message)
        self._set_session_status(session, f"{session.name}: {message}")
        self.log(f"[{session.name}] {message}")

    def _attempt_reconnect(self, session: ServerSession) -> None:
        if (
            session not in self._sessions
            or session.connected
            or session.denied
            or self._quitting
        ):
            return
        # Deliberately not _connect_session: no performance reset, no tab
        # steal, no inventory "attempt" spam — just the wire attempt. The
        # stored token makes a successful reconnect promptless.
        self._set_session_status(
            session, f"Reconnecting to {session.name} … (attempt {session.reconnect_attempts})"
        )
        session.client.connect_to(session.host, session.port)

    def _cancel_reconnect(self, session: ServerSession) -> None:
        if session.reconnect_timer is not None:
            session.reconnect_timer.stop()
        session.reconnect_attempts = 0

    def _request_server_log(self) -> None:
        session = self._session_for_page(self._tabs.currentWidget())
        if session is None:
            connected = [s for s in self._sessions if s.connected]
            session = connected[-1] if connected else None
        if session is None:
            self.log("Not connected — no server to request a log from")
            return
        session.client.request_log()

    def _show_server_log(self, server_name: str, text: str) -> None:
        title = (
            f'Log from server "{server_name}"' if server_name else "Log from server"
        )
        PeerLogDialog(title, text, self).show()

    def _on_frame(self, session: ServerSession, image) -> None:
        if session not in self._sessions:
            return
        session.frame_count += 1
        session.viewer.show_frame(image)
        self._set_session_status(
            session,
            f"Viewing {self._server_label(session)} — {image.width()}x{image.height()} — "
            f"{session.frame_count} frames received",
        )


def main() -> None:  # pragma: no cover - runs the Qt event loop
    minimized = "--minimized" in sys.argv[1:]
    log_path = logs.init_logging("remotedesktop")
    icon.set_windows_app_id("remotedesktop")
    app = QApplication(sys.argv)
    app.setWindowIcon(icon.app_icon("app"))
    # Closing the window while sharing hides to the tray instead of quitting;
    # every real exit path calls QApplication.quit() explicitly.
    app.setQuitOnLastWindowClosed(False)
    guard = SingleInstance()
    if not guard.acquire():
        # The running instance was asked to show itself; nothing to do here.
        raise SystemExit(0)
    Autostart().migrate_legacy()  # pre-1.0 server-only registration
    window = MainWindow()
    guard.activateRequested.connect(window.bring_to_front)
    window.log(f"Detailed log: {log_path}")
    if minimized and window.sharing_tab.serving and window._tray is not None:
        # Login-started while sharing: live in the tray until summoned.
        window._tray_notified = True  # no balloon for a start nobody clicked
    else:
        window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":  # pragma: no cover
    main()
