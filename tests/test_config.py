from remotedesktop.config import KnownServers, PairedClients, load_client_identity


def test_client_identity_is_created_and_stable(tmp_path) -> None:
    path = tmp_path / "identity.json"
    client_id, name = load_client_identity(path)
    assert client_id and name
    assert load_client_identity(path) == (client_id, name)


def test_pairing_issues_and_persists_a_token(tmp_path) -> None:
    path = tmp_path / "paired.json"
    paired = PairedClients(path)
    assert "some-id" not in paired
    token = paired.pair("some-id")
    assert token
    reloaded = PairedClients(path)
    assert "some-id" in reloaded
    assert reloaded.token_for("some-id") == token


def test_paired_clients_tolerate_corrupt_file(tmp_path) -> None:
    path = tmp_path / "paired.json"
    path.write_text("not json at all")
    assert "some-id" not in PairedClients(path)


def test_known_servers_round_trip(tmp_path) -> None:
    path = tmp_path / "known.json"
    known = KnownServers(path)
    assert known.get("host:1") is None
    known.remember("host:1", "fingerprint-abc", "token-xyz")
    record = KnownServers(path).get("host:1")
    assert record == {"fingerprint": "fingerprint-abc", "token": "token-xyz"}
