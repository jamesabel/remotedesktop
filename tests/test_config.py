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


def test_boolean_settings_round_trip_and_tolerate_legacy_values(tmp_path) -> None:
    settings = Settings(db.connect(tmp_path / "app.db"))
    # set_bool stores the historical "1"/"0" on-disk form.
    settings.set_bool("flag", True)
    assert settings.get("flag") == "1"
    assert settings.get_bool("flag") is True
    settings.set_bool("flag", False)
    assert settings.get("flag") == "0"
    assert settings.get_bool("flag") is False
    # Missing keys take the caller's default.
    assert settings.get_bool("missing") is False
    assert settings.get_bool("missing", True) is True
    # Other truthy spellings parse too (tobool), and junk never raises —
    # it falls back to the default.
    settings.set("flag", "true")
    assert settings.get_bool("flag") is True
    settings.set("flag", "junk-value")
    assert settings.get_bool("flag", True) is True
    assert settings.get_bool("flag", False) is False


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


def test_pairing_can_be_revoked(tmp_path) -> None:
    path = tmp_path / "app.db"
    paired = PairedClients(db.connect(path))
    paired.pair("some-id")
    paired.revoke("some-id")
    assert "some-id" not in PairedClients(db.connect(path))


def test_known_servers_round_trip(tmp_path) -> None:
    path = tmp_path / "app.db"
    known = KnownServers(db.connect(path))
    assert known.get("host:1") is None
    known.remember("host:1", "fingerprint-abc", "token-xyz")
    record = KnownServers(db.connect(path)).get("host:1")
    assert record == {"fingerprint": "fingerprint-abc", "token": "token-xyz"}


def test_known_server_can_be_forgotten(tmp_path) -> None:
    path = tmp_path / "app.db"
    known = KnownServers(db.connect(path))
    known.remember("host:1", "fp", "tok")
    known.forget("host:1")
    assert KnownServers(db.connect(path)).get("host:1") is None
