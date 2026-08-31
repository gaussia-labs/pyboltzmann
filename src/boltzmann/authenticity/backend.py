"""The only module that imports ``cryptography``, and the only seam the extra buys.

Everything else in this package is framing -- SSH wire strings, armor, fingerprints, records --
and works on a bare install. This module holds exactly one operation: the Ed25519 verify. It is
imported lazily and confined here so there is one place to audit and one seam to stub in tests.

The Ed25519 equation is deliberately not hand-rolled. RFC 8032 verification has cofactor edge
cases that a naive port gets wrong *silently*, so ``cryptography`` remains the primitive and a
mixed-order vector pins its strict, cofactorless equation. The small boundary below only decodes
points to reject non-canonical encodings, small-order public keys, and scalars outside the group
order before backend behaviour can vary. It never decides that a signature is valid.
"""

from __future__ import annotations

from boltzmann.exceptions import SignatureFormatError, VerificationUnavailableError

ED25519_SIGNATURE_BYTES = 64
"""Length of a raw Ed25519 signature: R and S, 32 bytes each."""

ED25519_FIELD = (1 << 255) - 19
"""Prime field modulus ``2^255 - 19``."""

ED25519_ORDER = (1 << 252) + 27742317777372353535851937790883648493
"""Order of Ed25519's prime-order subgroup."""

_ED25519_D = (-121665 * pow(121666, ED25519_FIELD - 2, ED25519_FIELD)) % ED25519_FIELD
_ED25519_SQRT_M1 = pow(2, (ED25519_FIELD - 1) // 4, ED25519_FIELD)


def _decode_point(encoded: bytes) -> tuple[int, int]:
    """Decode one canonical compressed Edwards25519 point or raise ``ValueError``."""
    if len(encoded) != 32:
        raise ValueError(f"expected 32 bytes, got {len(encoded)}")
    sign = encoded[31] >> 7
    y = int.from_bytes(encoded, "little") & ((1 << 255) - 1)
    if y >= ED25519_FIELD:
        raise ValueError("the compressed y-coordinate is not canonical")

    y_squared = y * y % ED25519_FIELD
    denominator = (_ED25519_D * y_squared + 1) % ED25519_FIELD
    if denominator == 0:
        raise ValueError("the compressed point is not on the Ed25519 curve")
    x_squared = (y_squared - 1) * pow(denominator, ED25519_FIELD - 2, ED25519_FIELD) % ED25519_FIELD
    x = pow(x_squared, (ED25519_FIELD + 3) // 8, ED25519_FIELD)
    if x * x % ED25519_FIELD != x_squared:
        x = x * _ED25519_SQRT_M1 % ED25519_FIELD
    if x * x % ED25519_FIELD != x_squared:
        raise ValueError("the compressed point is not on the Ed25519 curve")
    if x == 0 and sign:
        raise ValueError("the compressed point uses the non-canonical negative-zero encoding")
    if x & 1 != sign:
        x = ED25519_FIELD - x
    return x, y


def _double(point: tuple[int, int]) -> tuple[int, int]:
    """Double an Edwards25519 point; the curve's complete addition law has no exceptions."""
    x, y = point
    x_squared = x * x % ED25519_FIELD
    y_squared = y * y % ED25519_FIELD
    product = _ED25519_D * x_squared * y_squared % ED25519_FIELD
    next_x = 2 * x * y * pow(1 + product, ED25519_FIELD - 2, ED25519_FIELD) % ED25519_FIELD
    next_y = (y_squared + x_squared) * pow(1 - product, ED25519_FIELD - 2, ED25519_FIELD) % ED25519_FIELD
    return next_x, next_y


def _has_small_order(point: tuple[int, int]) -> bool:
    """Whether ``[8]point`` is the identity, covering the full cofactor subgroup."""
    for _ in range(3):
        point = _double(point)
    return point == (0, 1)


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
    try:
        public_point = _decode_point(key_data)
    except ValueError as error:
        raise SignatureFormatError(f"not an Ed25519 public key: {error}") from error
    if _has_small_order(public_point):
        raise SignatureFormatError("not an Ed25519 public key: the point has small order")
    if len(signature) != ED25519_SIGNATURE_BYTES:
        return False
    try:
        _decode_point(signature[:32])
    except ValueError:
        return False
    if int.from_bytes(signature[32:], "little") >= ED25519_ORDER:
        return False

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
