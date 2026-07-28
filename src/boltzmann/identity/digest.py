"""The three levels of hashes (paper Section 6.4).

A recurring source of confusion is that the same cryptographic algorithm is used
for three different kinds of identity. This module gives each one its own type so
the confusion becomes a type error instead of a subtle bug:

* :class:`BlockId` is **knowledge identity** -- a logical unit of knowledge.
* :class:`MerkleRoot` is the identity of a **logical snapshot** -- the exact
  composition of a module.
* :class:`OciDigest` is the identity of a transportable **file or manifest**.

They are deliberately not interchangeable. Passing a ``MerkleRoot`` where a
``BlockId`` is expected fails under mypy and, if it reaches runtime through
untyped data, raises :class:`~boltzmann.exceptions.DigestKindError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self

from pydantic_core import core_schema

from boltzmann.exceptions import DigestFormatError, DigestKindError
from boltzmann.identity.hashing import ALGORITHM, HEX_DIGEST_LENGTH, sha256_hex

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler

DIGEST_PATTERN = re.compile(rf"^{ALGORITHM}:[0-9a-f]{{{HEX_DIGEST_LENGTH}}}$")
"""A digest is ``sha256:`` followed by 64 lowercase hex characters."""


@dataclass(frozen=True, slots=True)
class Digest:
    """
    A content-addressed identifier: an algorithm and a hex digest.

    Subclasses carry no extra data. They exist so that the level of identity a
    value belongs to travels with the value itself.

    Attributes:
        algorithm (str): The hash algorithm. Only ``sha256`` in this version.
        hex (str): The lowercase hex-encoded digest.
    """

    algorithm: str
    hex: str

    KIND: ClassVar[str] = "digest"
    """Human-readable name of this level of identity, used in error messages."""

    def __post_init__(self) -> None:
        if self.algorithm != ALGORITHM:
            raise DigestFormatError(f"unsupported hash algorithm {self.algorithm!r}, expected {ALGORITHM!r}")
        if not re.fullmatch(rf"[0-9a-f]{{{HEX_DIGEST_LENGTH}}}", self.hex):
            raise DigestFormatError(f"malformed {self.KIND}: expected {HEX_DIGEST_LENGTH} lowercase hex characters")

    # --- Construction ---------------------------------------------------------

    @classmethod
    def of(cls, data: bytes) -> Self:
        """
        Compute the digest of ``data``.

        Args:
            data (bytes): The bytes to address.

        Returns:
            Self: An instance of the calling subclass.
        """
        return cls(algorithm=ALGORITHM, hex=sha256_hex(data))

    @classmethod
    def from_raw(cls, raw: bytes) -> Self:
        """
        Build a digest from raw digest bytes.

        Args:
            raw (bytes): The 32 raw digest bytes.

        Returns:
            Self: An instance of the calling subclass.
        """
        return cls(algorithm=ALGORITHM, hex=raw.hex())

    @classmethod
    def parse(cls, value: Any) -> Self:
        """
        Parse a ``<algorithm>:<hex>`` string into this level of identity.

        Args:
            value (Any): A digest string, or an instance of this exact class.

        Returns:
            Self: An instance of the calling subclass.

        Raises:
            DigestKindError: If ``value`` is a digest of a different level.
            DigestFormatError: If ``value`` is not a well-formed digest string.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, Digest):
            raise DigestKindError(f"expected a {cls.KIND}, got a {value.KIND} ({value})")
        if not isinstance(value, str):
            raise DigestFormatError(f"expected a {cls.KIND} string, got {type(value).__name__}")
        if not DIGEST_PATTERN.match(value):
            raise DigestFormatError(f"malformed {cls.KIND}: {value!r}")
        algorithm, _, hex_digest = value.partition(":")
        return cls(algorithm=algorithm, hex=hex_digest)

    # --- Access ---------------------------------------------------------------

    @property
    def raw(self) -> bytes:
        """The raw digest bytes, as Merkle hashing consumes them."""
        return bytes.fromhex(self.hex)

    @property
    def short(self) -> str:
        """An abbreviated form for logs and error messages."""
        return f"{self.algorithm}:{self.hex[:12]}"

    def __str__(self) -> str:
        return f"{self.algorithm}:{self.hex}"

    # --- Pydantic integration -------------------------------------------------

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        """Accept an instance of this exact class or its string form; emit the string form."""
        from_str = core_schema.chain_schema(
            [
                core_schema.str_schema(pattern=DIGEST_PATTERN.pattern),
                core_schema.no_info_plain_validator_function(cls.parse),
            ]
        )
        return core_schema.json_or_python_schema(
            json_schema=from_str,
            python_schema=core_schema.union_schema([core_schema.is_instance_schema(cls), from_str]),
            serialization=core_schema.plain_serializer_function_ser_schema(
                str, return_schema=core_schema.str_schema(), when_used="always"
            ),
        )


@dataclass(frozen=True, slots=True)
class BlockId(Digest):
    """
    Knowledge identity: the hash of a block's canonical serialization.

    ``block_id = SHA-256(canonical_serialization(block))``. If the content
    changes, a new block is born; the previous one may be kept for history,
    audit, or rollback (paper Section 6.1).
    """

    KIND: ClassVar[str] = "block_id"


@dataclass(frozen=True, slots=True)
class MerkleRoot(Digest):
    """
    Snapshot identity: the root of a module's internal Merkle DAG.

    Because the root depends only on the blocks and their structure, it is a
    canonical identity of a knowledge state independent of how that state is
    stored or transported. Two parties that assembled the same blocks obtain the
    same root (paper Section 6.2).
    """

    KIND: ClassVar[str] = "merkle_root"


@dataclass(frozen=True, slots=True)
class OciDigest(Digest):
    """
    Physical identity: the digest of a published OCI blob or manifest.

    Unlike a :class:`MerkleRoot`, this identifies a particular transportable
    file, not a logical composition (paper Section 6.4).
    """

    KIND: ClassVar[str] = "oci_digest"
