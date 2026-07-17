"""A running inventory of every peer this app has seen on the LAN.

Both apps keep one `ConnectionInventory`: the server records every client that
connects or attempts to, the client records every server it discovers or tries
to reach. `InventoryTab` shows it as a table and, optionally, a button to act
on the selected peer (revoke a client / forget a server). Persisted in SQLite,
so it answers "who is using this on the LAN, and who tried?" across restarts.
"""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import astuple, dataclass, fields

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from remotedesktop import db

# Event -> the state to display for the peer after it happens.
_EVENT_STATE = {
    "discovered": "discovered",
    "attempt": "connecting",
    "connected": "connected",
    "authenticated": "connected (token)",
    "paired": "connected (paired)",
    "denied": "denied",
    "refused": "refused",
    "disconnected": "disconnected",
    "revoked": "revoked",
    "forgotten": "forgotten",
    "error": "error",
}


@dataclass
class PeerRecord:
    key: str
    name: str
    address: str
    detail: str
    first_seen: str
    last_seen: str
    attempts: int
    state: str
    last_event: str


class ConnectionInventory(QObject):
    """Peers seen on the LAN, backed by SQLite so it persists across restarts.

    Pass a `sqlite3.Connection` from `db.connect`; omit it for an in-memory
    database that lives only as long as the object (used by tests).
    """

    changed = Signal()

    _COLUMNS = [f.name for f in fields(PeerRecord)]

    def __init__(
        self,
        connection: sqlite3.Connection | None = None,
        table: str = "peers",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        if table not in db.PEER_TABLES:
            raise ValueError(f"unknown inventory table: {table!r}")
        self._table = table
        self._peers: dict[str, PeerRecord] = {}
        self._db = connection if connection is not None else db.connect(None)
        self._load()

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _load(self) -> None:
        try:
            rows = self._db.execute(f"SELECT {', '.join(self._COLUMNS)} FROM {self._table}")
            for row in rows:
                record = PeerRecord(*row)
                self._peers[record.key] = record
        except sqlite3.Error:
            self._peers.clear()

    def _save(self, record: PeerRecord) -> None:
        placeholders = ", ".join("?" for _ in self._COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in self._COLUMNS if c != "key")
        try:
            self._db.execute(
                f"INSERT INTO {self._table} ({', '.join(self._COLUMNS)}) "
                f"VALUES ({placeholders}) ON CONFLICT(key) DO UPDATE SET {updates}",
                astuple(record),
            )
            self._db.commit()
        except sqlite3.Error:
            pass  # persistence failure must never break connectivity

    def record(
        self,
        key: str,
        event: str,
        *,
        name: str = "",
        address: str = "",
        detail: str = "",
    ) -> None:
        now = self._now()
        record = self._peers.get(key)
        if record is None:
            record = PeerRecord(key, name, address, detail, now, now, 0, "", "")
            self._peers[key] = record
        record.last_seen = now
        if name:
            record.name = name
        if address:
            record.address = address
        if detail:
            record.detail = detail
        if event == "attempt":
            record.attempts += 1
        record.state = _EVENT_STATE.get(event, event)
        record.last_event = event
        self._save(record)
        self.changed.emit()

    def peers(self) -> list[PeerRecord]:
        return sorted(self._peers.values(), key=lambda r: r.last_seen, reverse=True)


class InventoryTab(QWidget):
    """Table view of a ConnectionInventory, refreshed as it changes.

    If `action_label`/`action_callback` are given, a button below the table is
    enabled when a row is selected and calls `action_callback(peer_key)`.
    """

    _COLUMNS = ["Name", "Address", "Identifier", "State", "Attempts", "First seen", "Last seen"]

    def __init__(
        self,
        inventory: ConnectionInventory,
        action_label: str | None = None,
        action_callback: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._inventory = inventory
        self._action_callback = action_callback
        # One extra headerless column on the right: with stretchLastSection it
        # absorbs the leftover width, so the data columns (including "Last
        # seen") stay sized to their contents.
        self._table = QTableWidget(0, len(self._COLUMNS) + 1)
        self._table.setHorizontalHeaderLabels(self._COLUMNS + [""])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)

        self._action_button: QPushButton | None = None
        if action_label and action_callback:
            self._action_button = QPushButton(action_label)
            self._action_button.setEnabled(False)
            self._action_button.clicked.connect(self._on_action)
            self._table.itemSelectionChanged.connect(self._on_selection_changed)
            layout.addWidget(self._action_button)

        inventory.changed.connect(self.refresh)
        self.refresh()

    def _selected_key(self) -> str | None:
        row = self._table.currentRow()
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_selection_changed(self) -> None:
        if self._action_button is not None:
            self._action_button.setEnabled(self._selected_key() is not None)

    def _on_action(self) -> None:
        key = self._selected_key()
        if key and self._action_callback is not None:
            self._action_callback(key)

    def refresh(self) -> None:
        peers = self._inventory.peers()
        self._table.setRowCount(len(peers))
        for row, peer in enumerate(peers):
            values = [
                peer.name or "(unknown)",
                peer.address,
                peer.detail,
                peer.state,
                str(peer.attempts),
                peer.first_seen,
                peer.last_seen,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, peer.key)
                self._table.setItem(row, column, item)
        if self._action_button is not None:
            self._action_button.setEnabled(self._selected_key() is not None)
