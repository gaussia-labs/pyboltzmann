"""The only module that imports ``cryptography``, and the only seam the extra buys.

Everything else in this package is framing -- SSH wire strings, armor, fingerprints, records --
and works on a bare install. This module holds exactly one operation: the Ed25519 verify. It is
imported lazily and confined here so there is one place to audit and one seam to stub in tests.

Ed25519 is deliberately not hand-rolled. RFC 8032 verification has cofactor and non-canonical-
encoding edge cases that a naive port gets wrong *silently*, and the golden vectors exist
precisely to catch silent divergence. The trade the codebase makes for gzip over zstd ("an extra
dependency for a few percent") does not transfer here: the trade is not a few percent, it is the
correctness of a security primitive.
"""

from __future__ import annotations

from boltzmann.exceptions import SignatureFormatError, VerificationUnavailableError

ED25519_SIGNATURE_BYTES = 64
"""Length of a raw Ed25519 signature: R and S, 32 bytes each."""


def _ed25519():
    """Import the Ed25519 primitives lazily, so the core stays installable without the extra."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ModuleNotFoundError as error:  # pragma: no cover - depends on install extras
        raise VerificationUnavailableError(
            "checking an SSHSIG signature needs the [authenticity] extra: pip install "
            "'pyboltzmann[authenticity]'. Integrity verification is unaffected and needs nothing."
        ) from error
    return ed25519


def signature_backend_available() -> bool:
    """
    Whether the Ed25519 primitive is importable.

    Callers use this to report ``unchecked`` instead of attempting a verify they know will
    raise; they never use it to skip a check silently.

    Returns:
        bool: Whether the ``[authenticity]`` extra is installed.
    """
    try:
        _ed25519()
    except VerificationUnavailableError:
        return False
    return True


def verify_ed25519(key_data: bytes, signature: bytes, message: bytes) -> bool:
    """
    Check a raw Ed25519 signature over ``message``.

    Args:
        key_data (bytes): The 32-byte public key point.
        signature (bytes): The 64-byte raw signature.
        message (bytes): The exact bytes that were signed -- for SSHSIG, the signed-data blob.

    Returns:
        bool: Whether the signature verifies. A malformed-length signature is ``False``, not an
        error: on the verification path "this could never have been produced by the key" and
        "the key did not produce this" call for the same verdict.

    Raises:
        SignatureFormatError: If ``key_data`` is not a valid Ed25519 public key. A bad *key* is
            a structural fault in whatever carried it, not a fact about the signature.
        VerificationUnavailableError: If the ``[authenticity]`` extra is not installed.
    """
    ed25519 = _ed25519()
    from cryptography.exceptions import InvalidSignature

    try:
        key = ed25519.Ed25519PublicKey.from_public_bytes(key_data)
    except ValueError as error:
        raise SignatureFormatError(f"not an Ed25519 public key: {error}") from error
    try:
        key.verify(signature, message)
    except InvalidSignature:
        return False
    return True
