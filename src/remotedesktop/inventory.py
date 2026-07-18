"""A running inventory of every peer this app has seen on the LAN.

Both apps keep one `ConnectionInventory`: the server records every client that
connects or attempts to, the client records every server it discovers or tries
to reach. `InventoryTab` shows it as a table and, optionally, a button on each
row to act on that peer (revoke a client / forget a server). Persisted in
SQLite, so it answers "who is using this on the LAN, and who tried?" across
restarts.
"""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import astuple, dataclass, fields

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
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

    If `action_label`/`action_callback` are given, every row carries its own
    button labeled `action_label` that calls `action_callback(peer_key)` for
    that row's peer.
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
        self._action_label = action_label
        self._action_callback = action_callback
        # One extra headerless column on the right: with stretchLastSection it
        # absorbs the leftover width, so the data columns (including "Last
        # seen") stay sized to their contents. When an action is configured it
        # doubles as the action column, hosting each row's button.
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

        inventory.changed.connect(self.refresh)
        self.refresh()

    def _row_button(self, row: int) -> QPushButton | None:
        """The action button in a row's trailing cell (None without actions)."""
        container = self._table.cellWidget(row, len(self._COLUMNS))
        return container.findChild(QPushButton) if container is not None else None

    def _make_row_button(self, key: str) -> QWidget:
        # The trailing column is stretched; wrap the button in a left-aligned
        # container so it keeps its natural size instead of filling the cell.
        button = QPushButton(self._action_label)
        assert self._action_callback is not None
        callback = self._action_callback
        button.clicked.connect(lambda _checked=False, k=key: callback(k))
        container = QWidget()
        box = QHBoxLayout(container)
        box.setContentsMargins(4, 1, 4, 1)
        box.addWidget(button)
        box.addStretch(1)
        return container

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
            if self._action_label and self._action_callback is not None:
                self._table.setCellWidget(
                    row, len(self._COLUMNS), self._make_row_button(peer.key)
                )
