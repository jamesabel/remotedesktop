"""The Remote Desktop app: view other computers, and optionally share this
one — both roles in a single window.

Viewing: the Servers dock discovers servers on the LAN; each connection is a
`ServerSession` shown in its own closable tab (`remotedesktop.client`).
Sharing: the "Server" tab groups everything server-related — the sharing
opt-in (`remotedesktop.server.SharingTab`) and both peer inventories. While
sharing is enabled, closing the window hides to the system tray and sharing
continues; quitting is in the tray menu. Only one instance runs per user
session (`single_instance`)."""

import logging
import sqlite3
import sys
import time

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QGroupBox,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QTabBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import __version__, compat, db, icon, logs, window_state
from remotedesktop.about import AboutTab
from remotedesktop.autostart import Autostart
from remotedesktop.client import DiscoveryPanel, ServerSession
from remotedesktop.clipboard import ClipboardSync
from remotedesktop.config import KnownServers, Settings, default_db_path, load_client_identity
from remotedesktop.discovery import DEFAULT_CONNECT_PORT, DISCOVERY_PORT, ServerInfo
from remotedesktop.inventory import ConnectionInventory, InventoryTab
from remotedesktop.logs import PeerLogDialog, read_log_tail
from remotedesktop.modal_loop import HTCLOSE, HTMAXBUTTON, HTMINBUTTON, ModalLoopPump
from remotedesktop.performance import PerformanceMonitor, PerformanceTab
from remotedesktop.preferences import PreferencesTab, load_performance_window_seconds
from remotedesktop.server import SharingTab
from remotedesktop.sharing import ShareClient
from remotedesktop.single_instance import SingleInstance
from remotedesktop.viewer import ViewerWidget

_log = logging.getLogger("remotedesktop.app")


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
    ) -> None:
        super().__init__()
        self.setWindowIcon(icon.app_icon("app"))
        # Tests inject a connection to a temp database; the app uses the default.
        self._db = connection if connection is not None else db.connect(default_db_path())
        self._settings = Settings(self._db)
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
        # never re-emits `changed` and cannot loop between the roles.
        self._clipboard = ClipboardSync(parent=self)
        self._known_servers = KnownServers(self._db)
        self._identity = load_client_identity(self._db)
        self._tray_available = (
            QSystemTrayIcon.isSystemTrayAvailable() if tray_available is None else tray_available
        )
        self._tray: QSystemTrayIcon | None = None
        self._tray_notified = False
        self._quitting = False

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
        self.sharing_tab.restartRequested.connect(self._restart_app)

        # One tab per server connection (inserted at the front, closable),
        # followed by the fixed tabs, which never get a close button.
        self._sessions: list[ServerSession] = []
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.currentChanged.connect(lambda _index: self._refresh_status_bar())
        # One "Server" tab holds everything server-related: the sharing
        # opt-in with its viewers, plus both peer inventories.
        server_tab = QWidget()
        server_layout = QVBoxLayout(server_tab)
        sharing_group = QGroupBox("Sharing this computer")
        QVBoxLayout(sharing_group).addWidget(self.sharing_tab)
        servers_group = QGroupBox("Servers on LAN")
        QVBoxLayout(servers_group).addWidget(
            InventoryTab(self.client_inventory, "Forget", self._forget_server)
        )
        clients_group = QGroupBox("Clients on LAN")
        QVBoxLayout(clients_group).addWidget(
            InventoryTab(self.server_inventory, "Revoke", self._revoke_client)
        )
        server_layout.addWidget(sharing_group, stretch=2)
        server_layout.addWidget(servers_group, stretch=1)
        server_layout.addWidget(clients_group, stretch=1)
        self._tabs.addTab(server_tab, "Server")
        self.performance_pages = QTabWidget()
        self.performance_pages.addTab(
            PerformanceTab(self.client_performance, local="client", remote="server"),
            "Viewing",
        )
        self.performance_pages.addTab(
            PerformanceTab(self.server_performance, local="server", remote="client"),
            "Sharing",
        )
        performance_tab = QWidget()
        performance_layout = QVBoxLayout(performance_tab)
        performance_layout.addWidget(self.performance_pages)
        self._tabs.addTab(performance_tab, "Performance")
        self.setCentralWidget(self._tabs)

        self.discovery_panel = DiscoveryPanel(self, is_self=self._is_own_server)
        servers_dock = QDockWidget("Servers", self)
        servers_dock.setWidget(self.discovery_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, servers_dock)

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
        log_tab = QWidget()
        log_layout = QVBoxLayout(log_tab)
        log_layout.addWidget(self.get_log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        log_layout.addWidget(self.get_client_log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        log_layout.addWidget(self.connection_log)
        self._tabs.addTab(log_tab, "Connection log")
        self.preferences_tab = PreferencesTab(
            self._settings,
            [self.client_performance, self.server_performance],
            autostart=autostart,
        )
        self.preferences_tab.statusMessage.connect(self.log)
        self._tabs.addTab(self.preferences_tab, "Preferences")
        self._tabs.addTab(AboutTab(), "About")
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

        self._update_window_title()
        self.statusBar().showMessage("Not connected")
        window_state.restore_geometry(self, self._settings, window_state.MAIN_GEOMETRY_KEY)
        self.log("Remote Desktop started")
        # Now that the log pane, tray state, and signal wiring exist, start
        # sharing if the persisted opt-in is on.
        self.sharing_tab.restore_sharing()
        # Tests pass auto_scan=False so window tests never broadcast on the LAN.
        if auto_scan:
            self.discovery_panel.refresh()

    # ------------------------------------------------------------- logging

    def log(self, message: str) -> None:
        # Everything shown in the Connection log pane also goes to the debug
        # log file (when main() enabled it), so it survives the window.
        _log.info(message)
        self.connection_log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {message}")

    # ------------------------------------------------------- window chrome

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

    def _quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        window_state.save_geometry(self, self._settings, window_state.MAIN_GEOMETRY_KEY)
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
        for session in self._sessions:
            session.client.close()
        self.sharing_tab.shutdown()
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
        self.log("Restarting: freeing ports and launching a new process")
        _log.info("Restart requested — relaunching %s -m remotedesktop", sys.executable)
        self.sharing_tab.shutdown()
        if not QProcess.startDetached(sys.executable, ["-m", "remotedesktop"]):
            # Extremely unlikely (sys.executable exists); the app stays open —
            # sharing is stopped, but the machine isn't left with nothing.
            self.log("Restart failed: could not launch a new process — restart manually")
            _log.error("QProcess.startDetached failed for %s", sys.executable)
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

    def _session_for_key(self, key: str) -> ServerSession | None:
        for session in self._sessions:
            if session.key == key:
                return session
        return None

    def _session_for_viewer(self, widget) -> ServerSession | None:
        for session in self._sessions:
            if session.viewer is widget:
                return session
        return None

    def _set_session_status(self, session: ServerSession, text: str) -> None:
        session.status_text = text
        if self._tabs.currentWidget() is session.viewer:
            self.statusBar().showMessage(text)

    def _refresh_status_bar(self) -> None:
        session = self._session_for_viewer(self._tabs.currentWidget())
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
        key = f"{server.host}:{server.port}"
        session = self._session_for_key(key)
        if session is not None and session.connected:
            self._tabs.setCurrentWidget(session.viewer)
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
        client = ShareClient(
            identity=self._identity,
            known_servers=self._known_servers,
            clipboard=self._clipboard,
            performance=self.client_performance,
            log_provider=lambda: read_log_tail("remotedesktop"),
            parent=self,
        )
        session = ServerSession(key, name, client, viewer)
        viewer.inputEvent.connect(lambda event, s=session: self._on_input_event(s, event))
        client.status.connect(lambda message, s=session: self.log(f"[{s.name}] {message}"))
        client.connected.connect(lambda server_name, s=session: self._on_connected(s, server_name))
        client.approvalPending.connect(lambda s=session: self._on_approval_pending(s))
        client.denied.connect(lambda reason, s=session: self._on_denied(s, reason))
        client.disconnected.connect(lambda s=session: self._on_disconnected(s))
        client.frameReceived.connect(lambda image, s=session: self._on_frame(s, image))
        client.logReceived.connect(lambda text, s=session: self._show_server_log(s.name, text))
        self._sessions.append(session)
        self._tabs.insertTab(len(self._sessions) - 1, viewer, session.name)
        return session

    def _connect_session(self, session: ServerSession, host: str, port: int) -> None:
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
        self._tabs.setCurrentWidget(session.viewer)
        self._set_session_status(
            session, f"Connecting to {session.name} ({session.key}) …"
        )
        session.client.connect_to(host, port)

    def _close_session(self, session: ServerSession) -> None:
        """Disconnect the session and remove its tab (tab close / forget)."""
        session.client.close()  # a synchronous disconnect signal may fire here
        index = self._sessions.index(session)
        self._sessions.pop(index)
        self._tabs.removeTab(index)
        if session.connected and not session.denied:
            self.client_inventory.record(session.key, "disconnected", name=session.name)
        session.connected = False
        session.client.deleteLater()
        session.viewer.deleteLater()
        self.log(f"Closed connection to {session.name} ({session.key})")
        self._update_window_title()
        self._refresh_status_bar()

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

    def _request_server_log(self) -> None:
        session = self._session_for_viewer(self._tabs.currentWidget())
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
