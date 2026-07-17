import pytest

from remotedesktop import db
from remotedesktop.inventory import ConnectionInventory, InventoryTab

from test_sharing import make_client, make_server, pump


def test_unknown_table_is_rejected(qapp):
    with pytest.raises(ValueError):
        ConnectionInventory(table="not_a_table")


def test_database_errors_never_break_the_inventory(qapp, tmp_path):
    connection = db.connect(tmp_path / "app.db")
    connection.close()
    # Loading from a dead connection yields an empty inventory...
    inv = ConnectionInventory(connection)
    assert inv.peers() == []
    # ...and recording still updates the in-memory state despite save failing.
    inv.record("peer", "attempt", name="Bob")
    assert inv.peers()[0].name == "Bob"


def test_inventory_tracks_attempts_and_state(qapp):
    inv = ConnectionInventory()
    inv.record("c1", "discovered", name="Alice", address="10.0.0.5:48654")
    inv.record("c1", "attempt", name="Alice")
    inv.record("c1", "connected", name="Alice")
    peers = inv.peers()
    assert len(peers) == 1
    assert peers[0].name == "Alice"
    assert peers[0].attempts == 1
    assert peers[0].state == "connected"


def test_inventory_counts_multiple_attempts_and_orders_recent_first(qapp):
    inv = ConnectionInventory()
    inv.record("a", "attempt", name="A")
    inv.record("b", "attempt", name="B")
    inv.record("a", "attempt", name="A")
    inv.record("a", "denied", name="A")
    by_key = {p.key: p for p in inv.peers()}
    assert by_key["a"].attempts == 2
    assert by_key["a"].state == "denied"
    assert by_key["b"].attempts == 1


def test_inventory_persists_across_restarts(qapp, tmp_path):
    path = tmp_path / "app.db"
    inv = ConnectionInventory(db.connect(path))
    inv.record("peer", "attempt", name="Bob", address="10.0.0.9:48654")
    inv.record("peer", "connected", name="Bob")

    # A fresh inventory on the same database sees the earlier peer.
    reopened = ConnectionInventory(db.connect(path))
    peers = reopened.peers()
    assert len(peers) == 1
    assert peers[0].name == "Bob"
    assert peers[0].attempts == 1
    assert peers[0].state == "connected"
    # And it keeps accumulating rather than starting over.
    reopened.record("peer", "attempt", name="Bob")
    assert reopened.peers()[0].attempts == 2


def test_inventory_tab_shows_rows(qapp):
    inv = ConnectionInventory()
    tab = InventoryTab(inv)
    assert tab._table.rowCount() == 0
    inv.record("a", "attempt", name="A", address="host:1")
    assert tab._table.rowCount() == 1
    item = tab._table.item(0, 0)
    assert item is not None and item.text() == "A"


def test_inventory_tab_stretches_a_spacer_not_the_last_data_column(qapp):
    inv = ConnectionInventory()
    inv.record("a", "attempt", name="A", address="host:1")
    tab = InventoryTab(inv)
    spacer = tab._table.columnCount() - 1
    assert spacer == len(InventoryTab._COLUMNS)  # one extra column on the right
    header_item = tab._table.horizontalHeaderItem(spacer)
    assert header_item is not None and header_item.text() == ""
    assert tab._table.item(0, spacer) is None  # never populated
    assert tab._table.horizontalHeader().stretchLastSection()


def test_inventory_tab_action_button_passes_selected_key(qapp):
    inv = ConnectionInventory()
    inv.record("client-42", "connected", name="Bob")
    acted = []
    tab = InventoryTab(inv, "Revoke", acted.append)
    button = tab._action_button
    assert button is not None
    # No selection yet -> button disabled and does nothing useful.
    assert not button.isEnabled()
    tab._table.selectRow(0)
    assert button.isEnabled()
    button.click()
    assert acted == ["client-42"]


def test_server_populates_inventory_via_peer_events(qapp, credentials, tmp_path):
    server = make_server(credentials, tmp_path, approve=lambda *_: True)
    inv = ConnectionInventory()
    server.peerEvent.connect(
        lambda e: inv.record(
            e["key"], e["event"], name=e.get("name", ""), address=e.get("address", "")
        )
    )
    client = make_client(tmp_path)
    connected = []
    client.connected.connect(connected.append)
    client.connect_to("127.0.0.1", server.port)
    try:
        pump(qapp, lambda: connected)
        peers = inv.peers()
        assert len(peers) == 1
        assert peers[0].name == "test-client"
        assert peers[0].attempts == 1
        assert "connected" in peers[0].state  # "connected (paired)"
    finally:
        client.close()
        server.close()
