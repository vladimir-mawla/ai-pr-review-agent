"""GitHub webhook HMAC-SHA256 signature verification.

Owns: deciding whether an inbound webhook request body was genuinely signed
with the shared secret, before any other code (parsing, enqueueing) touches
the payload. Why this matters: the webhook endpoint is a public, unauthenticated
HTTP route — anyone who finds the URL can POST a forged ``pull_request`` event.
HMAC verification over the raw body is the only thing standing between a
forged request and a "real" review job being enqueued.

This module is deliberately narrow: it takes bytes + a header value + a
secret and raises one of three distinct exceptions, or returns normally. It
has no knowledge of FastAPI, HTTP status codes, or the payload shape — that
mapping belongs to the router.
"""

from __future__ import annotations

import hashlib
import hmac

_SIGNATURE_PREFIX = "sha256="
_DIGEST_HEX_LENGTH = 64  # SHA-256 produces 32 bytes == 64 hex characters.


class SignatureError(Exception):
    """Base class for all webhook signature verification failures."""


class MissingSignatureError(SignatureError):
    """The X-Hub-Signature-256 header was not present on the request."""


class MalformedSignatureError(SignatureError):
    """The header was present but not a well-formed ``sha256=<64 hex>`` value."""


class InvalidSignatureError(SignatureError):
    """The header was well-formed but does not match the computed HMAC."""


def verify_signature(raw_body: bytes, signature_header: str | None, secret: str) -> None:
    """Verify a GitHub webhook HMAC-SHA256 signature over the raw request body.

    Args:
        raw_body: The exact bytes of the HTTP request body, as received on
            the wire. This MUST be the raw bytes, not a re-serialization of
            a parsed JSON object — re-encoding (key order, whitespace,
            unicode escaping) changes the byte sequence and would make a
            correctly-signed request appear invalid.
        signature_header: The raw value of the ``X-Hub-Signature-256``
            request header, or ``None`` if it was absent.
        secret: The shared webhook secret, sourced from configuration.

    Raises:
        MissingSignatureError: ``signature_header`` is ``None``.
        MalformedSignatureError: the header does not have the
            ``sha256=<64 hex characters>`` shape (wrong prefix, wrong
            length, or non-hexadecimal characters).
        InvalidSignatureError: the header is well-formed but the digest does
            not match what we compute from ``raw_body`` and ``secret``.
    """
    if signature_header is None:
        raise MissingSignatureError("X-Hub-Signature-256 header is missing")

    if not signature_header.startswith(_SIGNATURE_PREFIX):
        raise MalformedSignatureError(
            f"signature header must start with {_SIGNATURE_PREFIX!r}"
        )

    provided_hex = signature_header[len(_SIGNATURE_PREFIX) :]
    if len(provided_hex) != _DIGEST_HEX_LENGTH or not _is_hex(provided_hex):
        raise MalformedSignatureError(
            f"signature must be exactly {_DIGEST_HEX_LENGTH} hexadecimal characters"
        )

    expected_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    # hmac.compare_digest compares in constant time regardless of where the
    # first differing byte falls. A plain `==` short-circuits at the first
    # mismatching byte, and an attacker who can measure response latency
    # precisely enough could exploit that to recover the correct signature
    # one byte at a time. This is the actual point of this module.
    if not hmac.compare_digest(expected_hex, provided_hex.lower()):
        raise InvalidSignatureError("signature does not match computed HMAC")


def _is_hex(value: str) -> bool:
    """Return True if every character in value is a hexadecimal digit."""
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
