"""A composition: the set of blocks that make up one version of a module.

Content addressing and immutable blocks make individual units append-only, but a
module version is a *composition* of those units, and compositions can exclude what
should no longer belong (paper Section 10).

This is the object every removal mechanism actually operates on. A drop does not
mutate a block; it produces a new composition, and therefore a new root.

A composition is also **persisted**, as :meth:`Composition.document`. The Merkle root commits
to a set of blocks but cannot be inverted back into it, so a snapshot that named only roots
would not be enough to reopen a brain: the leaf list has to be stored. That document is exactly
what a module layer carries when the brain is published.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import AppendOnlyViolationError, ModuleError
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.identity.serialization import canonicalize
from boltzmann.merkle.diff import CompositionDiff, diff
from boltzmann.merkle.proof import InclusionProof
from boltzmann.merkle.tree import MerkleTree, sorted_leaves


class Composition:
    """
    An immutable set of block identities, committed by a Merkle root.

    Every operation returns a new ``Composition`` rather than mutating this one, so
    that a root already handed to a consumer can never change underneath it.

    Attributes:
        memory_type (MemoryType): Which module this composition belongs to.
    """

    def __init__(self, memory_type: MemoryType, block_ids: Iterable[BlockId] = ()) -> None:
        """
        Build a composition.

        Args:
            memory_type (MemoryType): The module this composition belongs to.
            block_ids (Iterable[BlockId]): The blocks that make up the version.
        """
        self.memory_type = memory_type
        self._tree = MerkleTree(block_ids)
        self._members = frozenset(self._tree.leaves)

    # --- Access ---------------------------------------------------------------

    @property
    def block_ids(self) -> list[BlockId]:
        """The composition in canonical leaf order."""
        return list(self._tree.leaves)

    @property
    def root(self) -> MerkleRoot:
        """The Merkle root that identifies this version of the module."""
        return self._tree.root

    @property
    def layout(self) -> str:
        """Identifier of the Merkle layout used to compute the root."""
        return self._tree.name

    def __len__(self) -> int:
        return len(self._members)

    def __iter__(self) -> Iterator[BlockId]:
        return iter(self._tree.leaves)

    def __contains__(self, block_id: object) -> bool:
        return block_id in self._members

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Composition):
            return NotImplemented
        return self.memory_type is other.memory_type and self._members == other._members

    def __hash__(self) -> int:
        return hash((self.memory_type, self._members))

    def __repr__(self) -> str:
        return f"Composition({self.memory_type.value}, n={len(self)}, root={self.root.short})"

    # --- Verification ---------------------------------------------------------

    def inclusion_proof(self, block_id: BlockId) -> InclusionProof:
        """
        Prove that a block belongs to this composition.

        Args:
            block_id (BlockId): The block whose membership is proven.

        Returns:
            InclusionProof: A proof of size ``O(log n)``.
        """
        return self._tree.inclusion_proof(block_id)

    def verify(self) -> bool:
        """
        Recompute every membership proof against the root.

        Returns:
            bool: Whether the composition is internally consistent.
        """
        return self._tree.verify()

    # --- Derivation -----------------------------------------------------------

    def add(self, block_ids: Iterable[BlockId]) -> Composition:
        """
        Derive a composition that also contains ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): Blocks to include.

        Returns:
            Composition: The new composition, with a new root.
        """
        return Composition(self.memory_type, self._members | set(block_ids))

    def drop(self, block_ids: Iterable[BlockId]) -> Composition:
        """
        Derive a composition that excludes ``block_ids``.

        The dropped blocks are not mutated. They become unreachable from the new
        root and are eligible for pruning once no retained root references them.

        Args:
            block_ids (Iterable[BlockId]): Blocks to exclude.

        Returns:
            Composition: The new composition, with a new root.

        Raises:
            AppendOnlyViolationError: If this module is append-only. The episodic
                module records what happened, so corrections append new episodes or
                supersession relations rather than rewriting the past.
        """
        if self.memory_type.is_append_only:
            raise AppendOnlyViolationError(
                f"the {self.memory_type.value} module is append-only: use supersession or demotion "
                f"instead of dropping, so the chronological record stays intact"
            )
        return Composition(self.memory_type, self._members - set(block_ids))

    def diff(self, other: Composition) -> CompositionDiff:
        """
        Compare this composition with a later one.

        Args:
            other (Composition): The later composition of the same module.

        Returns:
            CompositionDiff: What was added, removed, and shared.

        Raises:
            ValueError: If the two compositions belong to different modules.
        """
        if self.memory_type is not other.memory_type:
            raise ValueError(
                f"cannot diff a {self.memory_type.value} composition against a {other.memory_type.value} one"
            )
        return diff(self, other)

    @staticmethod
    def canonical_order(block_ids: Iterable[BlockId]) -> list[BlockId]:
        """
        Order blocks the way a composition does.

        Args:
            block_ids (Iterable[BlockId]): Blocks to order.

        Returns:
            list[BlockId]: The blocks, deduplicated and ordered by digest.
        """
        return sorted_leaves(block_ids)

    # --- Persistence ----------------------------------------------------------

    def document(self) -> bytes:
        """
        Serialize the composition so it can be stored and shipped.

        This is what a module layer carries: the memory type, the Merkle layout, and the leaf
        list in canonical order. Storing it is not redundant with the root -- the root commits
        to the set but cannot be inverted into it, so without this document a snapshot could be
        verified and not reopened.

        Returns:
            bytes: The canonically serialized composition document.
        """
        return canonicalize(
            {
                "boltzmann": PROTOCOL_VERSION,
                "memory_type": self.memory_type.value,
                "layout": self.layout,
                "block_ids": [str(block_id) for block_id in self.block_ids],
            }
        )

    @classmethod
    def from_document(cls, data: bytes) -> Composition:
        """
        Rebuild a composition from its stored document.

        Args:
            data (bytes): A composition document.

        Returns:
            Composition: The composition it describes.

        Raises:
            ModuleError: If the document is malformed, names an unknown memory type, or was
                produced by a Merkle layout this client does not implement.
        """
        try:
            document: Any = json.loads(data)
        except json.JSONDecodeError as error:
            raise ModuleError(f"composition document is not valid JSON: {error}") from error

        if not isinstance(document, dict):
            raise ModuleError(f"composition document must be an object, got {type(document).__name__}")

        if document.get("boltzmann") != PROTOCOL_VERSION:
            raise ModuleError(
                f"composition document declares protocol version {document.get('boltzmann')!r}, "
                f"this client implements {PROTOCOL_VERSION}"
            )

        try:
            memory_type = MemoryType(document["memory_type"])
        except (KeyError, ValueError) as error:
            raise ModuleError(f"composition document names an unknown memory type: {error}") from error

        composition = cls(memory_type, (BlockId.parse(value) for value in document.get("block_ids", [])))

        layout = document.get("layout")
        if layout != composition.layout:
            raise ModuleError(
                f"composition was built with Merkle layout {layout!r}, this client implements "
                f"{composition.layout!r}: the roots would not be comparable"
            )
        return composition
