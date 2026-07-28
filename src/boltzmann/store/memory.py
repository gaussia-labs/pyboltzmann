"""An in-memory store, for tests and for running the conformance suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.exceptions import BlockNotFoundError, BlockTombstonedError
from boltzmann.identity.digest import Digest, OciDigest
from boltzmann.store.base import AbstractBlockStore

if TYPE_CHECKING:
    from collections.abc import Iterator


class MemoryBlockStore(AbstractBlockStore):
    """
    Holds content-addressed bytes in a dictionary.

    Useful as the reference against which a persistent store is compared: the two
    must be indistinguishable through the :class:`~boltzmann.store.base.BlockStore`
    interface.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._tombstones: dict[str, str] = {}
        self._pointers: dict[str, bytes] = {}

    def put_bytes(self, data: bytes) -> OciDigest:
        """
        Store bytes and return their content address.

        Args:
            data (bytes): The bytes to store.

        Returns:
            OciDigest: The content address of the stored bytes.
        """
        digest = OciDigest.of(data)
        self._blobs.setdefault(digest.hex, data)
        return digest

    def _load(self, digest: Digest) -> bytes:
        if digest.hex in self._tombstones:
            raise BlockTombstonedError(f"{digest.KIND} {digest.short} was redacted: {self._tombstones[digest.hex]}")
        try:
            return self._blobs[digest.hex]
        except KeyError:
            raise BlockNotFoundError(f"{digest.KIND} {digest.short} is not in this store") from None

    def has(self, digest: Digest) -> bool:
        """
        Whether the digest is known, resolvable or tombstoned.

        Args:
            digest (Digest): The content address to look for.

        Returns:
            bool: Whether the digest is known.
        """
        return digest.hex in self._blobs or digest.hex in self._tombstones

    def is_resolvable(self, digest: Digest) -> bool:
        """
        Whether the bytes behind the digest can still be read.

        Args:
            digest (Digest): The content address to check.

        Returns:
            bool: Whether the bytes are present and not tombstoned.
        """
        return digest.hex in self._blobs and digest.hex not in self._tombstones

    def tombstone(self, digest: Digest, reason: str) -> None:
        """
        Destroy the bytes while keeping the identity.

        Args:
            digest (Digest): The content address to redact.
            reason (str): Why the bytes were destroyed.
        """
        self._blobs.pop(digest.hex, None)
        self._tombstones[digest.hex] = reason

    def delete(self, digest: Digest) -> None:
        """
        Reclaim the bytes outright.

        Args:
            digest (Digest): The content address to reclaim.
        """
        self._blobs.pop(digest.hex, None)
        self._tombstones.pop(digest.hex, None)

    def iter_digests(self) -> Iterator[OciDigest]:
        """
        Iterate every resolvable digest held.

        Returns:
            Iterator[OciDigest]: The content addresses present.
        """
        for hex_digest in list(self._blobs):
            yield OciDigest.parse(f"sha256:{hex_digest}")

    def read_pointer(self, name: str) -> bytes | None:
        """
        Read a named mutable pointer.

        Args:
            name (str): Pointer name.

        Returns:
            bytes | None: The stored value, or ``None`` if unset.
        """
        return self._pointers.get(name)

    def write_pointer(self, name: str, data: bytes) -> None:
        """
        Set a named mutable pointer.

        Args:
            name (str): Pointer name.
            data (bytes): The value to store.
        """
        self._pointers[name] = data

    def tombstoned(self) -> dict[OciDigest, str]:
        """
        The digests whose bytes were destroyed, and why.

        Returns:
            dict[OciDigest, str]: Redacted digests mapped to their recorded reason.
        """
        return {OciDigest.parse(f"sha256:{hex_digest}"): reason for hex_digest, reason in self._tombstones.items()}

    def __len__(self) -> int:
        return len(self._blobs)
