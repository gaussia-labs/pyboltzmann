"""The content-addressed store interface.

Two levels of content live in one physical store, and the distinction matters:

* **Blobs** are transportable bytes -- an observed PDF, a normalized view, an index
  file. They are addressed by :class:`~boltzmann.identity.digest.OciDigest`.
* **Blocks** are units of knowledge. They are addressed by
  :class:`~boltzmann.identity.digest.BlockId`, which is the hash of the block's
  canonically serialized envelope.

Reading bytes is level-agnostic, because physical resolution does not care what a
digest means -- this is the "hash map: immediate physical resolution" row of the
paper's index table (Section 6.3). Reading a *block* is not: it decodes, verifies
the canonical form, and hands back a typed object.

A store never decides what belongs to a module. Membership is the composition's
business (:mod:`boltzmann.module.composition`); a store only answers whether it
holds the bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from boltzmann.blocks.base import Block
from boltzmann.exceptions import BlockIntegrityError
from boltzmann.identity.digest import BlockId, Digest, OciDigest
from boltzmann.identity.hashing import sha256_hex


@runtime_checkable
class BlockStore(Protocol):
    """Holds content-addressed bytes and the blocks decoded from them."""

    # --- Physical layer -------------------------------------------------------

    def put_bytes(self, data: bytes) -> OciDigest:
        """
        Store bytes and return their content address.

        Storing identical bytes twice is a no-op: the digest is the same, so there is
        nothing new to write.

        Args:
            data (bytes): The bytes to store.

        Returns:
            OciDigest: The content address of the stored bytes.
        """
        ...

    def get_bytes(self, digest: Digest) -> bytes:
        """
        Retrieve bytes by content address, verifying integrity.

        Args:
            digest (Digest): Any level of digest. The caller's type expresses what
                the bytes mean; physical resolution treats them alike.

        Returns:
            bytes: The stored bytes.

        Raises:
            BlockNotFoundError: If the store does not hold the bytes.
            BlockTombstonedError: If the bytes were redacted.
            BlockIntegrityError: If the stored bytes do not hash to ``digest``.
        """
        ...

    def has(self, digest: Digest) -> bool:
        """
        Whether the store knows this digest at all, resolvable or tombstoned.

        Args:
            digest (Digest): The content address to look for.

        Returns:
            bool: Whether the digest is known.
        """
        ...

    def is_resolvable(self, digest: Digest) -> bool:
        """
        Whether the bytes behind a digest can still be read.

        A conforming implementation must report which blocks of a snapshot are
        resolvable and which are tombstoned, so that a removed block is never
        indistinguishable from a corrupted one (paper Section 10.6).

        Args:
            digest (Digest): The content address to check.

        Returns:
            bool: Whether the bytes are present and not tombstoned.
        """
        ...

    def tombstone(self, digest: Digest, reason: str) -> None:
        """
        Destroy the bytes behind a digest while keeping its identity.

        Redaction punches a hole in a composition that still names the block: the
        Merkle DAG references identities, not bytes, so membership still verifies but
        reconstruction of that one block is forfeited (paper Section 10.6).

        Args:
            digest (Digest): The content address to redact.
            reason (str): Why the bytes were destroyed, for the audit record.
        """
        ...

    def delete(self, digest: Digest) -> None:
        """
        Reclaim bytes that no retained root references.

        This is the mechanical half of pruning. Deciding *what* is unreachable is the
        retention layer's job; a store only reclaims what it is told to.

        Args:
            digest (Digest): The content address to reclaim.
        """
        ...

    def iter_digests(self) -> Iterator[OciDigest]:
        """
        Iterate every digest the store holds.

        Returns:
            Iterator[OciDigest]: The content addresses present, in unspecified order.
        """
        ...

    # --- Knowledge layer ------------------------------------------------------

    def put_block(self, block: Block) -> BlockId:
        """
        Store a block's canonical serialization.

        Args:
            block (Block): The block to store.

        Returns:
            BlockId: The block's content-addressed identity.
        """
        ...

    def get_block(self, block_id: BlockId) -> Block:
        """
        Retrieve and decode a block.

        Args:
            block_id (BlockId): The block's identity.

        Returns:
            Block: The decoded, typed block.

        Raises:
            BlockNotFoundError: If the store does not hold the block.
            BlockTombstonedError: If the block was redacted.
            BlockIntegrityError: If the stored bytes are not canonical or do not hash
                to ``block_id``.
        """
        ...

    # --- Mutable pointers -----------------------------------------------------

    def read_pointer(self, name: str) -> bytes | None:
        """
        Read a named mutable pointer, such as the current snapshot.

        Content is immutable, but a brain still needs one mutable cell: which snapshot is
        current. Keeping it outside the content-addressed space is what lets a commit be
        atomic -- blobs are written first and the pointer moves last, so a crash leaves
        orphan blobs a prune can reclaim rather than a half-applied version.

        Args:
            name (str): Pointer name.

        Returns:
            bytes | None: The stored value, or ``None`` if unset.
        """
        ...

    def write_pointer(self, name: str, data: bytes) -> None:
        """
        Set a named mutable pointer.

        Args:
            name (str): Pointer name.
            data (bytes): The value to store.
        """
        ...


class AbstractBlockStore(ABC):
    """
    Base for the stores this SDK ships.

    Subclasses implement byte-level persistence; the knowledge level and every
    integrity check are implemented once, here. A third-party store does not have to
    inherit from this class -- satisfying :class:`BlockStore` is enough -- but doing so
    means the verification rules cannot drift.
    """

    # --- To implement ---------------------------------------------------------

    @abstractmethod
    def put_bytes(self, data: bytes) -> OciDigest:
        """Persist bytes and return their content address."""

    @abstractmethod
    def _load(self, digest: Digest) -> bytes:
        """
        Read the stored bytes for a digest.

        Raises:
            BlockNotFoundError: If the digest is unknown.
            BlockTombstonedError: If the bytes were redacted.
        """

    @abstractmethod
    def has(self, digest: Digest) -> bool:
        """Whether the digest is known, resolvable or tombstoned."""

    @abstractmethod
    def is_resolvable(self, digest: Digest) -> bool:
        """Whether the bytes behind the digest can still be read."""

    @abstractmethod
    def tombstone(self, digest: Digest, reason: str) -> None:
        """Destroy the bytes while keeping the identity."""

    @abstractmethod
    def delete(self, digest: Digest) -> None:
        """Reclaim the bytes outright."""

    @abstractmethod
    def iter_digests(self) -> Iterator[OciDigest]:
        """Iterate every digest held."""

    @abstractmethod
    def read_pointer(self, name: str) -> bytes | None:
        """Read a named mutable pointer, or ``None`` if unset."""

    @abstractmethod
    def write_pointer(self, name: str, data: bytes) -> None:
        """Set a named mutable pointer."""

    # --- Implemented once -----------------------------------------------------

    def get_bytes(self, digest: Digest) -> bytes:
        """
        Retrieve bytes by content address, verifying that they hash to it.

        Args:
            digest (Digest): The content address to resolve.

        Returns:
            bytes: The stored bytes.

        Raises:
            BlockIntegrityError: If the stored bytes do not hash to ``digest``.
        """
        data = self._load(digest)
        actual = sha256_hex(data)
        if actual != digest.hex:
            raise BlockIntegrityError(
                f"stored bytes for {digest.KIND} {digest.short} hash to sha256:{actual[:12]}: the store is corrupt"
            )
        return data

    def put_block(self, block: Block) -> BlockId:
        """
        Store a block's canonical serialization.

        Args:
            block (Block): The block to store.

        Returns:
            BlockId: The block's content-addressed identity.
        """
        self.put_bytes(block.canonical_bytes())
        return block.block_id

    def get_block(self, block_id: BlockId) -> Block:
        """
        Retrieve and decode a block.

        Args:
            block_id (BlockId): The block's identity.

        Returns:
            Block: The decoded, typed block.
        """
        return Block.decode(self.get_bytes(block_id))

    def __contains__(self, digest: object) -> bool:
        return isinstance(digest, Digest) and self.has(digest)
