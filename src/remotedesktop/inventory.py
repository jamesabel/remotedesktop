"""A running inventory of every peer this app has seen on the LAN.

Both apps keep one `ConnectionInventory`: the server records every client that
connects or attempts to, the client records every server it discovers or tries
to reach. `InventoryTab` shows it as a table. The inventory is in-memory and
covers the current run — enough to answer "who is using this on the LAN right
now, and who tried?" for a solo developer or a small team.
"""

import time
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

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
    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._peers: dict[str, PeerRecord] = {}

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

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
        self.changed.emit()

    def peers(self) -> list[PeerRecord]:
        return sorted(self._peers.values(), key=lambda r: r.last_seen, reverse=True)


class InventoryTab(QWidget):
    """Table view of a ConnectionInventory, refreshed as it changes."""

    _COLUMNS = ["Name", "Address", "Identifier", "State", "Attempts", "First seen", "Last seen"]

    def __init__(self, inventory: ConnectionInventory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._inventory = inventory
        self._table = QTableWidget(0, len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setStretchLastSection(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self._table)
        inventory.changed.connect(self.refresh)
        self.refresh()

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
                self._table.setItem(row, column, QTableWidgetItem(value))
