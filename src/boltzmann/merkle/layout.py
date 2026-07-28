"""The Merkle layout interface.

The paper fixes that a module version is committed by a Merkle root, and leaves the
construction open. This SDK ships one layout -- RFC 6962 over sorted leaves, see
:mod:`boltzmann.merkle.tree` -- behind this interface, so that an implementation
which needs literal structural sharing of internal nodes (a prolly tree or a HAMT,
as Dolt and IPLD use) can substitute its own without touching the layers above.

Any conforming layout must satisfy two properties:

1. **The root is a function of the set.** Two parties that assembled the same
   blocks obtain the same root, whatever order they were added in (paper
   Section 6.2). A layout whose root depends on insertion order is not conforming.
2. **Membership is provable in ``O(log n)``.**
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.identity.digest import BlockId, MerkleRoot
    from boltzmann.merkle.proof import InclusionProof


@runtime_checkable
class MerkleLayout(Protocol):
    """Commits a set of blocks to a single verifiable root."""

    @property
    def name(self) -> str:
        """Identifier of the layout, recorded alongside a snapshot."""
        ...

    def root(self, block_ids: Iterable[BlockId]) -> MerkleRoot:
        """
        Compute the root that commits to ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): The composition of a module.

        Returns:
            MerkleRoot: The identity of that composition.
        """
        ...

    def inclusion_proof(self, block_ids: Iterable[BlockId], target: BlockId) -> InclusionProof:
        """
        Build a proof that ``target`` belongs to ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): The composition of a module.
            target (BlockId): The block whose membership is proven.

        Returns:
            InclusionProof: The proof, of size ``O(log n)``.
        """
        ...
