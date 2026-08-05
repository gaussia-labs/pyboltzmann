"""SHA-256 primitives and domain separation constants.

The protocol uses one algorithm for three different kinds of identity
(paper Section 6.4), so every hash computed here states what it is hashing.
Domain separation prefixes keep a leaf hash from ever colliding with an
internal node hash, which is what closes the second-preimage attack that a
naive Merkle tree admits (CVE-2012-2459).
"""

import hashlib

ALGORITHM = "sha256"
"""The only hash algorithm this version of the protocol defines."""

DIGEST_SIZE = 32
"""Length in bytes of a SHA-256 digest."""

HEX_DIGEST_LENGTH = DIGEST_SIZE * 2
"""Length in characters of a hex-encoded SHA-256 digest."""

LEAF_PREFIX = b"\x00"
"""Domain separation prefix for a Merkle leaf (RFC 9162, Section 2.1.1)."""

NODE_PREFIX = b"\x01"
"""Domain separation prefix for a Merkle internal node (RFC 9162, Section 2.1.1)."""


def sha256(data: bytes) -> bytes:
    """
    Compute the raw SHA-256 digest of ``data``.

    Args:
        data (bytes): The bytes to hash.

    Returns:
        bytes: The 32-byte digest.
    """
    return hashlib.sha256(data).digest()


def sha256_hex(data: bytes) -> str:
    """
    Compute the hex-encoded SHA-256 digest of ``data``.

    Args:
        data (bytes): The bytes to hash.

    Returns:
        str: The 64-character lowercase hex digest.
    """
    return hashlib.sha256(data).hexdigest()


def hash_leaf(digest: bytes) -> bytes:
    """
    Hash a Merkle leaf with its domain separation prefix.

    Args:
        digest (bytes): The raw digest that forms the leaf.

    Returns:
        bytes: ``SHA-256(0x00 || digest)``.
    """
    return sha256(LEAF_PREFIX + digest)


def hash_node(left: bytes, right: bytes) -> bytes:
    """
    Hash a Merkle internal node with its domain separation prefix.

    Args:
        left (bytes): The left child hash.
        right (bytes): The right child hash.

    Returns:
        bytes: ``SHA-256(0x01 || left || right)``.
    """
    return sha256(NODE_PREFIX + left + right)


def hash_empty() -> bytes:
    """
    Hash the empty Merkle tree.

    Returns:
        bytes: ``SHA-256("")``, which is ``MTH({})`` in RFC 9162, Section 2.1.1.
    """
    return sha256(b"")
