"""Scheduler-scoped TLS policy and Pixii private-CA compatibility."""

from __future__ import annotations

import re
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509

from ..exceptions import SchedulerConfigurationError

_CA_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def normalize_scheduler_ca_filename(value: object) -> str | None:
    """Return a safe /config/ssl basename or None for invalid input."""
    if not isinstance(value, str):
        return None
    filename = value.strip()
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or not _CA_FILENAME_RE.fullmatch(filename)
    ):
        return None
    return filename


def build_scheduler_ssl_context(
    *,
    pixii_ca_compatibility: bool = False,
    ca_file: str | Path | None = None,
) -> ssl.SSLContext:
    """Build the scheduler-only TLS context without weakening normal TLS policy."""
    context = ssl.create_default_context()
    if not pixii_ca_compatibility:
        return context

    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", None)
    if strict_flag is None:
        raise SchedulerConfigurationError("compatibility_unsupported")
    if ca_file is None:
        raise SchedulerConfigurationError("ca_file_missing")

    path = Path(ca_file)
    certificate_data = _read_certificate_data(path)
    certificate = _parse_certificate(certificate_data)
    _validate_ca_certificate(certificate)
    try:
        context.load_verify_locations(cafile=str(path))
    except (OSError, ssl.SSLError, ValueError):
        raise SchedulerConfigurationError("ca_file_invalid") from None

    original_flags = context.verify_flags
    original_minimum_version = context.minimum_version
    original_ciphers = _cipher_policy(context)
    context.verify_flags = original_flags & ~strict_flag

    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise SchedulerConfigurationError("compatibility_unsupported")
    if context.verify_flags != original_flags & ~strict_flag:
        raise SchedulerConfigurationError("compatibility_unsupported")
    if context.minimum_version != original_minimum_version:
        raise SchedulerConfigurationError("compatibility_unsupported")
    if _cipher_policy(context) != original_ciphers:
        raise SchedulerConfigurationError("compatibility_unsupported")
    return context


def scheduler_tls_diagnostics(
    context: ssl.SSLContext,
    *,
    pixii_ca_compatibility: bool,
) -> dict[str, object]:
    """Return only scheduler TLS policy state safe for diagnostics."""
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", None)
    strict_enabled = strict_flag is not None and bool(context.verify_flags & strict_flag)
    return {
        "tls_mode": "pixii_ca_compatibility" if pixii_ca_compatibility else "system_default",
        "custom_ca_configured": pixii_ca_compatibility,
        "x509_strict_enabled": strict_enabled,
        "cert_required": context.verify_mode == ssl.CERT_REQUIRED,
        "hostname_verification": context.check_hostname,
    }


def default_scheduler_tls_diagnostics() -> dict[str, object]:
    """Return in-memory default scheduler TLS state without touching the system."""
    return {
        "tls_mode": "system_default",
        "custom_ca_configured": False,
        "x509_strict_enabled": getattr(ssl, "VERIFY_X509_STRICT", None) is not None,
        "cert_required": True,
        "hostname_verification": True,
    }


def _read_certificate_data(path: Path) -> bytes:
    try:
        certificate_data = path.read_bytes()
    except FileNotFoundError:
        raise SchedulerConfigurationError("ca_file_missing") from None
    except OSError:
        raise SchedulerConfigurationError("ca_file_unreadable") from None
    if not certificate_data or b"PRIVATE KEY" in certificate_data.upper():
        raise SchedulerConfigurationError("ca_file_invalid")
    return certificate_data


def _parse_certificate(certificate_data: bytes) -> x509.Certificate:
    try:
        if b"-----BEGIN" in certificate_data:
            return x509.load_pem_x509_certificate(certificate_data)
        return x509.load_der_x509_certificate(certificate_data)
    except (TypeError, ValueError):
        raise SchedulerConfigurationError("ca_file_invalid") from None


def _validate_ca_certificate(certificate: x509.Certificate) -> None:
    try:
        basic_constraints = certificate.extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
    except x509.ExtensionNotFound:
        raise SchedulerConfigurationError("ca_not_ca") from None
    if not basic_constraints.ca:
        raise SchedulerConfigurationError("ca_not_ca")

    now = datetime.now(UTC)
    not_before = getattr(certificate, "not_valid_before_utc", None)
    not_after = getattr(certificate, "not_valid_after_utc", None)
    if not isinstance(not_before, datetime) or not isinstance(not_after, datetime):
        not_before = certificate.not_valid_before.replace(tzinfo=UTC)
        not_after = certificate.not_valid_after.replace(tzinfo=UTC)
    if not_before > now or not_after < now:
        raise SchedulerConfigurationError("ca_file_invalid")


def _cipher_policy(context: ssl.SSLContext) -> tuple[tuple[tuple[str, Any], ...], ...]:
    """Represent the context cipher policy for an invariant check."""
    return tuple(
        tuple(sorted(cipher.items())) for cipher in context.get_ciphers()
    )
