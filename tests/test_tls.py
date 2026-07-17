from remotedesktop import tls


def test_credentials_are_created_then_reloaded(tmp_path):
    cert_path = tmp_path / "certs" / "server_cert.pem"
    key_path = tmp_path / "certs" / "server_key.pem"

    cert, key = tls.load_or_create_credentials(cert_path, key_path)
    assert cert_path.exists() and key_path.exists()
    assert not cert.isNull() and not key.isNull()

    # A second load reuses the same persisted identity.
    cert2, _key2 = tls.load_or_create_credentials(cert_path, key_path)
    assert tls.certificate_fingerprint(cert2) == tls.certificate_fingerprint(cert)


def test_distinct_certificates_have_distinct_fingerprints(qapp, credentials):
    other_cert, _ = tls.ephemeral_credentials()
    assert tls.certificate_fingerprint(credentials[0]) != tls.certificate_fingerprint(other_cert)
