"""Types for the commit, which is the only write path in the protocol.

For each accepted block an implementation serializes it canonically, computes its ``block_id``,
stores it as an immutable object, connects it to its source via provenance, incorporates it into
the episodic, semantic, or procedural Merkle DAG, updates the indices, and creates a new
snapshot (paper Section 8.3).

Those seven steps are one transaction, and that requirement is what a conforming implementation
owes the caller. A block that reached the store but not the composition would be a block no root
commits to; a composition that advanced without its provenance edges would be knowledge with no
auditable origin. Either outcome breaks a guarantee the protocol makes, so a failure part-way
through must leave the previous snapshot as the current one.

:class:`CommitResult` is the shape that report takes. The operation itself is declared on
:class:`~boltzmann.protocol.operations.BrainWriter`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.module.snapshot import Snapshot


class CommitResult(BaseModel):
    """
    What a commit changed.

    Attributes:
        snapshot (Snapshot): The new state of the brain.
        committed (list[BlockId]): Blocks added to a composition.
        provenance (list[BlockId]): Provenance entries written alongside them.
        roots (dict[MemoryType, MerkleRoot]): The new root of each module the commit touched. A
            single commit can advance several modules at once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: Snapshot
    committed: list[BlockId] = Field(default_factory=list)
    provenance: list[BlockId] = Field(default_factory=list)
    roots: dict[MemoryType, MerkleRoot] = Field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Whether the commit changed nothing, as when every candidate was a duplicate."""
        return not self.committed
