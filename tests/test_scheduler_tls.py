from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.pixiihome_controller.exceptions import SchedulerConfigurationError
from custom_components.pixiihome_controller.scheduler.tls import (
    build_scheduler_ssl_context,
    normalize_scheduler_ca_filename,
    scheduler_tls_diagnostics,
)


def _write_certificate(path: Path, *, ca: bool) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Sanitized Test CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def test_normal_scheduler_context_keeps_default_tls_policy() -> None:
    expected = ssl.create_default_context()
    context = build_scheduler_ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verify_flags == expected.verify_flags
    assert context.minimum_version == expected.minimum_version
    assert context.get_ciphers() == expected.get_ciphers()


@pytest.mark.skipif(
    not hasattr(ssl, "VERIFY_X509_STRICT"), reason="runtime has no X509 strict verification flag"
)
def test_pixii_compatibility_removes_only_strict_flag(tmp_path: Path) -> None:
    ca_file = tmp_path / "sanitized-ca.crt"
    _write_certificate(ca_file, ca=True)
    expected = ssl.create_default_context()
    context = build_scheduler_ssl_context(
        pixii_ca_compatibility=True,
        ca_file=ca_file,
    )

    strict = ssl.VERIFY_X509_STRICT
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verify_flags == expected.verify_flags & ~strict
    assert bool(context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN) == bool(
        expected.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    )
    assert context.minimum_version == expected.minimum_version
    assert context.get_ciphers() == expected.get_ciphers()
    assert scheduler_tls_diagnostics(context, pixii_ca_compatibility=True) == {
        "tls_mode": "pixii_ca_compatibility",
        "custom_ca_configured": True,
        "x509_strict_enabled": False,
        "cert_required": True,
        "hostname_verification": True,
    }


def test_safe_ca_filename_rejects_traversal_and_paths() -> None:
    for value in ("", "../pixii.crt", "/tmp/pixii.crt", "C:\\pixii.crt", "subdir/pixii.crt"):
        assert normalize_scheduler_ca_filename(value) is None
    assert normalize_scheduler_ca_filename("pixii-ca.crt") == "pixii-ca.crt"


def test_missing_invalid_non_ca_and_private_key_files_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(SchedulerConfigurationError, match="ca_file_missing"):
        build_scheduler_ssl_context(pixii_ca_compatibility=True, ca_file=tmp_path / "missing.crt")

    invalid = tmp_path / "invalid.crt"
    invalid.write_text("not a certificate", encoding="ascii")
    with pytest.raises(SchedulerConfigurationError, match="ca_file_invalid"):
        build_scheduler_ssl_context(pixii_ca_compatibility=True, ca_file=invalid)

    leaf = tmp_path / "leaf.crt"
    _write_certificate(leaf, ca=False)
    with pytest.raises(SchedulerConfigurationError, match="ca_not_ca"):
        build_scheduler_ssl_context(pixii_ca_compatibility=True, ca_file=leaf)

    private_key = tmp_path / "private.key"
    private_key.write_text("-----BEGIN PRIVATE KEY-----\n", encoding="ascii")
    with pytest.raises(SchedulerConfigurationError, match="ca_file_invalid"):
        build_scheduler_ssl_context(pixii_ca_compatibility=True, ca_file=private_key)


def test_compatibility_requires_ca_filename_and_fails_closed_without_strict(monkeypatch) -> None:
    with pytest.raises(SchedulerConfigurationError, match="ca_file_missing"):
        build_scheduler_ssl_context(pixii_ca_compatibility=True)

    monkeypatch.setattr(
        ssl,
        "VERIFY_X509_STRICT",
        None,
        raising=False,
    )
    with pytest.raises(SchedulerConfigurationError, match="compatibility_unsupported"):
        build_scheduler_ssl_context(pixii_ca_compatibility=True, ca_file="sanitized-ca.crt")
