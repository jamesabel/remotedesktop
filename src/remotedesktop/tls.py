"""TLS credentials for the server and the SSL configuration for both ends.

The server has no CA, so it generates a self-signed certificate on first run
and persists it. The client trusts the certificate on first connection and
pins its fingerprint (trust-on-first-use, like SSH); a later fingerprint
change for the same server is treated as a possible impersonation. Because the
certificate is self-signed, the usual chain-of-trust verification is turned
off and identity is enforced by the pinned fingerprint instead.
"""

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PySide6.QtCore import QCryptographicHash
from PySide6.QtNetwork import QSsl, QSslCertificate, QSslConfiguration, QSslKey, QSslSocket


def generate_self_signed(common_name: str = "remotedesktop") -> tuple[bytes, bytes]:
    """Return (certificate PEM, private key PEM) for a fresh self-signed cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def load_or_create_credentials(
    cert_path: Path, key_path: Path
) -> tuple[QSslCertificate, QSslKey]:
    """Load the server's cert/key PEM files, generating them if absent."""
    if not (cert_path.exists() and key_path.exists()):
        cert_pem, key_pem = generate_self_signed()
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)
    else:
        cert_pem = cert_path.read_bytes()
        key_pem = key_path.read_bytes()
    return QSslCertificate(cert_pem), QSslKey(key_pem, QSsl.KeyAlgorithm.Rsa)


def ephemeral_credentials() -> tuple[QSslCertificate, QSslKey]:
    """A fresh in-memory cert/key, for a server with no persisted identity."""
    cert_pem, key_pem = generate_self_signed()
    return QSslCertificate(cert_pem), QSslKey(key_pem, QSsl.KeyAlgorithm.Rsa)


def certificate_fingerprint(cert: QSslCertificate) -> str:
    """Stable SHA-256 fingerprint of a certificate, as lowercase hex."""
    digest = QCryptographicHash.hash(cert.toDer(), QCryptographicHash.Algorithm.Sha256)
    return bytes(digest).hex()


def server_configuration(cert: QSslCertificate, key: QSslKey) -> QSslConfiguration:
    config = QSslConfiguration.defaultConfiguration()
    config.setLocalCertificate(cert)
    config.setPrivateKey(key)
    config.setProtocol(QSsl.SslProtocol.TlsV1_2OrLater)
    # The server does not ask clients for a certificate; clients authenticate
    # with the paired-token challenge instead.
    config.setPeerVerifyMode(QSslSocket.PeerVerifyMode.VerifyNone)
    return config


def client_configuration() -> QSslConfiguration:
    config = QSslConfiguration.defaultConfiguration()
    config.setProtocol(QSsl.SslProtocol.TlsV1_2OrLater)
    # Self-signed server cert: skip chain verification and pin the fingerprint.
    config.setPeerVerifyMode(QSslSocket.PeerVerifyMode.VerifyNone)
    return config
