from remotedesktop import db
from remotedesktop.config import (
    KnownServers,
    PairedClients,
    Settings,
    load_client_identity,
)


def test_settings_round_trip(tmp_path) -> None:
    path = tmp_path / "app.db"
    Settings(db.connect(path)).set("theme", "dark")
    assert Settings(db.connect(path)).get("theme") == "dark"
    assert Settings(db.connect(path)).get("missing", "fallback") == "fallback"


def test_client_identity_is_created_and_stable(tmp_path) -> None:
    path = tmp_path / "app.db"
    client_id, name = load_client_identity(db.connect(path))
    assert client_id and name
    # A fresh connection to the same database returns the same identity.
    assert load_client_identity(db.connect(path)) == (client_id, name)


def test_pairing_issues_and_persists_a_token(tmp_path) -> None:
    path = tmp_path / "app.db"
    paired = PairedClients(db.connect(path))
    assert "some-id" not in paired
    token = paired.pair("some-id")
    assert token
    reloaded = PairedClients(db.connect(path))
    assert "some-id" in reloaded
    assert reloaded.token_for("some-id") == token


def test_known_servers_round_trip(tmp_path) -> None:
    path = tmp_path / "app.db"
    known = KnownServers(db.connect(path))
    assert known.get("host:1") is None
    known.remember("host:1", "fingerprint-abc", "token-xyz")
    record = KnownServers(db.connect(path)).get("host:1")
    assert record == {"fingerprint": "fingerprint-abc", "token": "token-xyz"}
