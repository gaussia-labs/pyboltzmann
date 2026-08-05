"""Inclusion proofs: membership without downloading the module.

Membership of a single block is proven with the sibling hashes along its path to
the root, a proof of size ``O(log n)`` (paper Section 6.2). This is what lets a
consumer verify that a block belongs to the installed snapshot without holding the
rest of the module.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from boltzmann.exceptions import InclusionProofError
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.identity.hashing import HEX_DIGEST_LENGTH, hash_leaf, hash_node

NODE_HASH_PATTERN = rf"^[0-9a-f]{{{HEX_DIGEST_LENGTH}}}$"

NodeHash = Annotated[str, StringConstraints(pattern=NODE_HASH_PATTERN)]
"""An intermediate Merkle node hash, hex-encoded.

Deliberately not a :class:`~boltzmann.identity.digest.Digest`. The three levels of
hashes identify *things* -- a unit of knowledge, a logical snapshot, a
transportable blob. An audit path element identifies nothing; it is scaffolding
that exists only to recompute a root, so giving it an identity type would blur the
distinction the three levels are there to keep sharp.
"""


class InclusionProof(BaseModel):
    """
    Proof that one block belongs to the composition committed by a root.

    Attributes:
        block_id (BlockId): The block whose membership is proven.
        leaf_index (int): Position of the block among the sorted leaves.
        tree_size (int): Number of leaves in the composition.
        audit_path (list[NodeHash]): Sibling hashes from the leaf up to the root.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: BlockId
    leaf_index: int = Field(ge=0)
    tree_size: int = Field(gt=0)
    audit_path: list[NodeHash]

    def verify(self, root: MerkleRoot) -> bool:
        """
        Recompute the root from this proof and compare.

        Args:
            root (MerkleRoot): The root the proof is checked against.

        Returns:
            bool: Whether the proof reconstructs ``root``.
        """
        if self.leaf_index >= self.tree_size:
            return False

        # RFC 9162, Section 2.1.3.2, which is where this algorithm is written down:
        # RFC 6962 defined the proof but left verification to the reader. ``fn``
        # tracks the position of the running hash within its level; ``sn`` tracks the
        # position of the last node at that level, which is how the algorithm
        # recognizes a right edge.
        position = self.leaf_index
        last = self.tree_size - 1
        running = hash_leaf(self.block_id.raw)

        for sibling in self.audit_path:
            if last == 0:
                return False
            sibling_hash = bytes.fromhex(sibling)
            if position & 1 or position == last:
                running = hash_node(sibling_hash, running)
                while not position & 1 and position != 0:
                    position >>= 1
                    last >>= 1
            else:
                running = hash_node(running, sibling_hash)
            position >>= 1
            last >>= 1

        return last == 0 and running == root.raw

    def require(self, root: MerkleRoot) -> None:
        """
        Verify the proof, raising if it does not hold.

        Args:
            root (MerkleRoot): The root the proof is checked against.

        Raises:
            InclusionProofError: If the proof does not reconstruct ``root``.
        """
        if not self.verify(root):
            raise InclusionProofError(
                f"block {self.block_id.short} is not proven to be in the composition committed by {root.short}"
            )


def is_node_hash(value: str) -> bool:
    """
    Whether ``value`` is a well-formed hex node hash.

    Args:
        value (str): The candidate string.

    Returns:
        bool: Whether it is 64 lowercase hex characters.
    """
    return re.match(NODE_HASH_PATTERN, value) is not None
