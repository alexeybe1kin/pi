import ssl

import pytest

from gateway.__main__ import create_certificate, renew_certificate


def test_tls_identity_survives_restart_and_only_explicit_renewal_replaces_it(tmp_path):
    cert, key = create_certificate(tmp_path, "localhost")
    original = cert.read_bytes(), key.read_bytes()
    create_certificate(tmp_path, "localhost")
    assert (cert.read_bytes(), key.read_bytes()) == original
    renew_certificate(tmp_path, "localhost")
    assert cert.read_bytes() != original[0] and key.read_bytes() != original[1]
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)


def test_incomplete_tls_material_fails_closed_instead_of_replacing_identity(tmp_path):
    _, key = create_certificate(tmp_path, "localhost")
    secret = key.read_bytes()
    (tmp_path / "tls.crt").unlink()
    with pytest.raises(ValueError, match="Restore both"):
        create_certificate(tmp_path, "localhost")
    assert key.read_bytes() == secret
