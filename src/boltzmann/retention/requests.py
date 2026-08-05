"""Types for the removal operations (paper Section 10).

Everything else in the protocol only adds. Content addressing and immutable blocks make individual
units append-only, but a module version is a *composition* of those units, and compositions can
exclude what should no longer belong.

The operations are declared on :class:`~boltzmann.protocol.operations.BrainRetention`; this module
defines what each one takes and what it reports. The four mechanisms are deliberately separate
types, because conflating them is the mistake Section 10.1 warns against:

**Drop** excludes blocks from a module's composition. It does not mutate blocks: a new Merkle DAG
is built over the survivors, a new root computed, the indices rebuilt, and the removal recorded in
provenance. Consumers of the new root never see the dropped block, while older retained roots keep
verifying exactly as before. That is what makes drop the cleanup path for wrong knowledge.

**Supersession and demotion** change accessibility, not membership. The block stays in the
composition and remains verifiable. This is the only path available to the episodic module.

**Pruning** reclaims bytes no retained root references. It never decides *what* to forget — a drop
already did.

**Redaction** destroys bytes that a retained root still names. It is for law and safety, not for
cleanup, and it forfeits reconstruction of that block.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, Producer, RemovalMechanism
from boltzmann.identity.digest import BlockId, Digest, MerkleRoot, OciDigest
from boltzmann.module.snapshot import Snapshot

# --- Drop ---------------------------------------------------------------------


class DropRequest(BaseModel):
    """
    An intent to exclude blocks from a module.

    Attributes:
        blocks (list[BlockId]): What to exclude.
        memory_type (MemoryType): Which module to exclude them from.
        actor (Actor): Who is dropping.
        reason (str): Why. Required, because an unexplained removal is not auditable.
        policy_name (str | None): The named policy invoked to authorize the drop.
        rederive_against (BlockId | None): A replacement canonical block to re-derive dependents
            from, instead of dropping them. Re-derivation is never the default: it runs only when
            the caller has registered a replacement or asks for one explicitly
            (paper Section 8.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    blocks: list[BlockId] = Field(min_length=1)
    memory_type: MemoryType
    actor: Actor
    reason: str = Field(min_length=1)
    policy_name: str | None = None
    rederive_against: BlockId | None = None


class ProducerDropRequest(BaseModel):
    """
    An intent to drop everything a given producer made: batch invalidation (Section 10.3).

    Because provenance records the producer of each derived block, a drop may be stated over a set
    -- everything from one ingestion, or everything derived by one model version. That is the
    natural response to deliberately wrong knowledge introduced in bulk, and it reuses the same
    cascade rather than inventing a second mechanism.

    Attributes:
        producer (Producer): Whose output to invalidate.
        memory_types (list[MemoryType]): Which modules to sweep.
        actor (Actor): Who is dropping.
        reason (str): Why.
        policy_name (str | None): The named policy invoked to authorize the drop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    producer: Producer
    memory_types: list[MemoryType] = Field(min_length=1)
    actor: Actor
    reason: str = Field(min_length=1)
    policy_name: str | None = None


class DropResult(BaseModel):
    """
    What a drop changed.

    Attributes:
        snapshot (Snapshot): The new state of the brain.
        dropped (dict[MemoryType, list[BlockId]]): What left each module. A canonical drop appears
            here alongside the derived blocks its cascade removed.
        roots (dict[MemoryType, MerkleRoot]): The new root of each module rewritten.
        provenance (list[BlockId]): The removal records written.
        review_required (bool): Whether the cascade exceeded the policy's threshold and the commit
            is being held for review.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: Snapshot
    dropped: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    roots: dict[MemoryType, MerkleRoot] = Field(default_factory=dict)
    provenance: list[BlockId] = Field(default_factory=list)
    review_required: bool = False


class CascadePlan(BaseModel):
    """
    What a drop would remove, computed before anything is written.

    Producing the plan separately is what lets a policy hold a large cascade for review instead of
    discovering its size after the fact.

    Dropping a semantic or procedural block rewrites or removes the provenance edges that pointed
    to it, and treats downstream dependents the same way; the canonical source remains, so a
    corrected block can be re-derived later. Dropping a canonical block is privileged: the
    dependency closure is always walked, and every derived block that listed the canonical as
    evidence is dropped by default in the same commit.

    Attributes:
        origin (BlockId): The block whose removal starts the cascade.
        origin_memory_type (MemoryType): Which module the origin belongs to.
        privileged (bool): Whether this is a canonical drop, which always cascades.
        dependents (dict[MemoryType, list[BlockId]]): What would be dropped, by module.
        rederivable (list[BlockId]): Dependents that could be re-derived instead, because a
            replacement canonical is available.
        provenance_edges (list[BlockId]): Provenance entries that would be rewritten or removed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: BlockId
    origin_memory_type: MemoryType
    privileged: bool
    dependents: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    rederivable: list[BlockId] = Field(default_factory=list)
    provenance_edges: list[BlockId] = Field(default_factory=list)

    @property
    def size(self) -> int:
        """How many blocks the cascade would drop, across every module."""
        return sum(len(blocks) for blocks in self.dependents.values())


# --- Supersession and demotion ------------------------------------------------


class SupersessionResult(BaseModel):
    """
    What a supersession or demotion recorded.

    Attributes:
        snapshot (Snapshot): The new state of the brain. Only the provenance module advanced; the
            module holding the superseded block did not, because its composition is unchanged.
        provenance (list[BlockId]): The records written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: Snapshot
    provenance: list[BlockId] = Field(default_factory=list)


# --- Pruning ------------------------------------------------------------------


class PruneReport(BaseModel):
    """
    What a prune found and reclaimed.

    Attributes:
        retained_roots (int): How many snapshots were treated as reachable.
        reachable (int): How many blocks are still referenced by some retained root.
        reclaimed (list[OciDigest]): What was actually deleted.
        dry_run (bool): Whether anything was deleted at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    retained_roots: int = Field(ge=0)
    reachable: int = Field(ge=0)
    reclaimed: list[OciDigest] = Field(default_factory=list)
    dry_run: bool = False

    @property
    def reclaimed_count(self) -> int:
        """How many blobs were reclaimed."""
        return len(self.reclaimed)


# --- Redaction ----------------------------------------------------------------


class RedactionResult(BaseModel):
    """
    What a redaction destroyed.

    Two limits of redaction are worth stating alongside the type. A hash of low-entropy content is
    not anonymous, so confirming a guess may still be possible while the ``block_id`` is kept. And
    erasure does not propagate by itself across already-pulled copies: the protocol can publish a
    revocation, but a distributed brain can only signal destruction, not guarantee it.

    Attributes:
        mechanism (RemovalMechanism): Which redaction was applied.
        redacted (list[Digest]): The digests whose bytes are gone. Their identities remain, and
            membership proofs over them still verify.
        provenance (list[BlockId]): The removal records written.
        snapshot (Snapshot): The state of the brain afterwards. Roots are unchanged, except for a
            lineage rewrite, which invalidates prior roots and publishes a new lineage.
        invalidates_prior_roots (bool): Whether consumers holding earlier roots can no longer
            verify them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism: RemovalMechanism
    redacted: list[Digest] = Field(default_factory=list)
    provenance: list[BlockId] = Field(default_factory=list)
    snapshot: Snapshot
    invalidates_prior_roots: bool = False


class ResolvabilityReport(BaseModel):
    """
    Which of a snapshot's bytes can still be read -- the blocks, and what they name.

    A conforming implementation must report which blocks of a snapshot are resolvable and which are
    tombstoned, so that a removed block is never indistinguishable from a corrupted one
    (paper Section 10.6). Keeping ``missing`` separate from ``tombstoned`` is that requirement.

    A block may name content it does not carry, and that content is as much a part of what the
    snapshot asserts as the envelope naming it: an episode whose transcript is gone is not a whole
    episode. So the same three-way split is reported for content, and by the same argument -- a
    transcript destroyed under an erasure policy must not read as a damaged store.

    The content of an unreadable block is not classified, because the digests it names can only be
    learned by reading it. A tombstoned block therefore contributes nothing here, which is correct:
    what its content is now is not knowable from the snapshot, and the tombstone already says the
    block is gone.

    Attributes:
        resolvable (dict[MemoryType, list[BlockId]]): Blocks whose bytes are present.
        tombstoned (dict[MemoryType, list[BlockId]]): Blocks whose bytes were destroyed under an
            erasure policy.
        missing (dict[MemoryType, list[BlockId]]): Blocks whose bytes are absent with no tombstone
            -- corruption rather than redaction.
        content_resolvable (dict[MemoryType, list[Digest]]): Content named by a readable block and
            present in the store.
        content_tombstoned (dict[MemoryType, list[Digest]]): Content destroyed under an erasure
            policy while the block naming it remains readable.
        content_missing (dict[MemoryType, list[Digest]]): Content named by a readable block and
            absent with no tombstone. The block verifies and the composition verifies; the datum it
            names is gone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resolvable: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    tombstoned: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    missing: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    content_resolvable: dict[MemoryType, list[Digest]] = Field(default_factory=dict)
    content_tombstoned: dict[MemoryType, list[Digest]] = Field(default_factory=dict)
    content_missing: dict[MemoryType, list[Digest]] = Field(default_factory=dict)

    @property
    def is_intact(self) -> bool:
        """Whether every block and every datum it names resolves or is accounted for by a tombstone."""
        return not any(self.missing.values()) and not any(self.content_missing.values())
