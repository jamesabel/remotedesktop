from remotedesktop.config import ApprovedClients, load_client_identity


def test_client_identity_is_created_and_stable(tmp_path) -> None:
    path = tmp_path / "identity.json"
    client_id, name = load_client_identity(path)
    assert client_id and name
    assert load_client_identity(path) == (client_id, name)


def test_approved_clients_persist(tmp_path) -> None:
    path = tmp_path / "approved.json"
    approved = ApprovedClients(path)
    assert "some-id" not in approved
    approved.add("some-id")
    assert "some-id" in ApprovedClients(path)


def test_approved_clients_tolerate_corrupt_file(tmp_path) -> None:
    path = tmp_path / "approved.json"
    path.write_text("not json at all")
    assert "some-id" not in ApprovedClients(path)
