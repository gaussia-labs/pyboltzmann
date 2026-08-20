"""Differencing two versions of a module.

The paper describes differencing as comparing roots top-down and descending only
where child hashes differ, so the cost is proportional to what changed
(paper Section 6.2). With sorted leaves and internal nodes derived rather than
stored, the same result comes from a set operation over the two persisted leaf
lists: exact, ``O(n)``, and with no tree to walk.

What an incremental update actually needs is the answer this returns -- which blocks
to fetch and which to forget -- not the shape of the path taken to compute it.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.merkle.tree import merkle_root, sorted_leaves


class CompositionDiff(BaseModel):
    """
    What changed between two compositions of the same module.

    Attributes:
        before (MerkleRoot): Root of the earlier composition.
        after (MerkleRoot): Root of the later composition.
        added (list[BlockId]): Blocks present only in ``after``.
        removed (list[BlockId]): Blocks present only in ``before``.
        unchanged (list[BlockId]): Blocks shared by both, reused by hash rather than
            retransmitted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    before: MerkleRoot
    after: MerkleRoot
    added: list[BlockId]
    removed: list[BlockId]
    unchanged: list[BlockId]

    @property
    def is_empty(self) -> bool:
        """Whether the two compositions are identical."""
        return not self.added and not self.removed

    @property
    def transfer_size(self) -> int:
        """
        How many blocks a consumer must fetch to move from ``before`` to ``after``.

        This is the number the paper's claim about incremental updates is about: the
        cost is proportional to what changed, because unchanged blocks are shared by
        hash instead of duplicated.
        """
        return len(self.added)


def diff(before: Iterable[BlockId], after: Iterable[BlockId]) -> CompositionDiff:
    """
    Compare two compositions of a module.

    Args:
        before (Iterable[BlockId]): The earlier composition.
        after (Iterable[BlockId]): The later composition.

    Returns:
        CompositionDiff: What was added, removed, and shared, plus both roots.
    """
    before_leaves = sorted_leaves(before)
    after_leaves = sorted_leaves(after)
    before_set = set(before_leaves)
    after_set = set(after_leaves)

    return CompositionDiff(
        before=merkle_root(before_leaves),
        after=merkle_root(after_leaves),
        added=sorted_leaves(after_set - before_set),
        removed=sorted_leaves(before_set - after_set),
        unchanged=sorted_leaves(before_set & after_set),
    )
