"""Provenance memory: where knowledge came from and how it was transformed.

The provenance module connects derived blocks to evidence, transformations, and
tool versions. It answers what must be recomputed when a source changes, and it is
the **ledger of removals**: every drop, cascade, and redaction is recorded here so
that exclusion stays auditable (paper Sections 5 and 10.4).

It is also where everything actor-dependent about a canonical registration lives --
who incorporated a source, when, from where, under what license, and which earlier
edition it supersedes -- because a canonical block's identity must depend only on
the bytes it describes (see :mod:`boltzmann.blocks.canonical`).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.identity.principal import parse_actor_id
from boltzmann.identity.time import Timestamp


class ActorKind(StrEnum):
    """What sort of agent performed an operation."""

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    PIPELINE = "pipeline"


class ProducerKind(StrEnum):
    """What produced a derived block.

    Because provenance records the producer of each derived block, a drop may be
    stated over a whole set: everything produced by a given ingestion, or
    everything derived by a given model version (paper Section 10.3).
    """

    MODEL = "model"
    PIPELINE = "pipeline"
    BATCH = "batch"
    ACTOR = "actor"


class ValidationStatus(StrEnum):
    """The verdict on one candidate (paper Section 10.3).

    Defined here rather than beside the gate that produces it, because a verdict is not a write-path
    ephemeral: it travels in a provenance record, so it is part of the wire schema. The gate re-exports
    it, and :mod:`boltzmann.ingest.validation` remains the place to import it from.
    """

    VALIDATED = "validated"
    """Well-formed, referenced, and consistent. Eligible for commit."""

    PENDING_REVIEW = "pending_review"
    """Admissible but not decidable by the protocol alone."""

    REJECTED = "rejected"
    """Malformed, unreferenced, or duplicate. Never committed."""

    CONTRADICTED = "contradicted"
    """Well-formed but in conflict with knowledge already held."""


class RemovalMechanism(StrEnum):
    """How knowledge left a brain (paper Section 10, Table 5).

    ``DROP`` excludes a block from a module's composition and is the ordinary path.
    ``SUPERSEDE`` and ``DEMOTE`` change accessibility, not membership. ``PRUNE``
    reclaims bytes no retained root needs. The remaining three are redaction: they
    destroy bytes that a retained root still names, and are reserved for law or
    safety policy.
    """

    DROP = "drop"
    SUPERSEDE = "supersede"
    DEMOTE = "demote"
    PRUNE = "prune"
    TOMBSTONE = "tombstone"
    CRYPTO_SHRED = "crypto_shred"
    LINEAGE_REWRITE = "lineage_rewrite"

    @property
    def is_redaction(self) -> bool:
        """Whether the mechanism destroys bytes a retained root still references."""
        return self in {
            RemovalMechanism.TOMBSTONE,
            RemovalMechanism.CRYPTO_SHRED,
            RemovalMechanism.LINEAGE_REWRITE,
        }


class Actor(BaseModel):
    """
    Who performed an operation.

    Attributes:
        id (str): Stable identifier of the actor.
        kind (ActorKind): What sort of agent it is.
        name (str | None): Human-readable label.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: ActorKind
    name: str | None = None


def _writing_actor(actor: Actor) -> Actor:
    """Refuse an actor a conforming writer would not record."""
    parse_actor_id(actor.id, field="actor id")
    return actor


WritingActor = Annotated[Actor, AfterValidator(_writing_actor)]
"""An :class:`Actor` on the way *in*, whose identifier must take one of the two accepted forms.

The asymmetry is the point, and it is the only arrangement that works. :class:`Actor` itself stays
permissive, because every provenance record ever written decodes through it and a validator on the
type would make every brain that predates this rule unreadable -- punishing readers for a writer's
old habit. Enforcement therefore attaches to the request models and to the brain handle, where a
new identifier is being *chosen* and a caller can still be told what to choose instead.

This is the producer/verifier split the protocol uses everywhere, pointed in the only direction
that works for a field nothing can verify: strict where the value is minted, tolerant where it is
read, and reported by :meth:`~boltzmann.brain.Brain.audit_attribution` in between.
"""


class Producer(BaseModel):
    """
    What produced a derived block, at the granularity a batch invalidation needs.

    Attributes:
        kind (ProducerKind): Model, pipeline, ingestion batch, or actor.
        id (str): Identifier, such as a model name or an ingestion id.
        version (str | None): Version of the producer, so a drop can name one
            version of a model without touching the others.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProducerKind
    id: str = Field(min_length=1)
    version: str | None = None


class RegistrationRecord(BaseModel):
    """
    A canonical source was incorporated.

    Attributes:
        record_type (Literal["registration"]): Discriminator.
        block (BlockId): The canonical block that was registered.
        actor (Actor): Who incorporated it.
        at (Timestamp): When it was incorporated.
        origin (str | None): Where it came from, such as a URL or a file path.
        license (str | None): License the source is held under.
        retention_policy (str | None): Named retention policy that governs it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["registration"] = "registration"
    block: BlockId
    actor: Actor
    at: Timestamp
    origin: str | None = None
    license: str | None = None
    retention_policy: str | None = None


class DerivationRecord(BaseModel):
    """
    A derived block was produced from canonical evidence.

    This is the edge a canonical drop walks to build its dependency closure.

    Attributes:
        record_type (Literal["derivation"]): Discriminator.
        block (BlockId): The derived block.
        derived_from (list[BlockId]): The evidence it cites. Never empty: a derived
            block with no evidence has no root to be audited against.
        producer (Producer): What produced it.
        actor (Actor): Who ran the production.
        at (Timestamp): When it was produced.
        task (str | None): Identifier of the processing task that yielded it.
        locator (str | None): Where in the source it came from, such as a page or a
            line range.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["derivation"] = "derivation"
    block: BlockId
    derived_from: list[BlockId] = Field(min_length=1)
    producer: Producer
    actor: Actor
    at: Timestamp
    task: str | None = None
    locator: str | None = None


class NormalizationRecord(BaseModel):
    """
    A normalized view was produced from an original blob.

    Attributes:
        record_type (Literal["normalization"]): Discriminator.
        block (BlockId): The canonical block holding the normalized view.
        pipeline (str): Name of the deterministic pipeline that produced it.
        pipeline_version (str): Version of that pipeline, so the transform can be
            reproduced or compared.
        actor (Actor): Who ran it.
        at (Timestamp): When it ran.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["normalization"] = "normalization"
    block: BlockId
    pipeline: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    actor: Actor
    at: Timestamp


class SupersessionRecord(BaseModel):
    """
    A block takes precedence over an earlier one.

    ``replace`` is register plus a supersession edge, with an optional drop of the
    old evidence -- not a mutation of bytes already stored (paper Section 8.1).

    Attributes:
        record_type (Literal["supersession"]): Discriminator.
        block (BlockId): The block that now takes precedence.
        supersedes (BlockId): The block it replaces.
        actor (Actor): Who declared the supersession.
        at (Timestamp): When it was declared.
        reason (str | None): Why the earlier block was superseded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["supersession"] = "supersession"
    block: BlockId
    supersedes: BlockId
    actor: Actor
    at: Timestamp
    reason: str | None = None


class DemotionRecord(BaseModel):
    """
    A block's retrieval priority was lowered without removing it.

    Cognitive psychology separates the availability of a trace from its accessibility, and demotion is
    that distinction: the block stays in the composition, still proves into the root, still resolves.
    What changes is that it stops competing for the top of every ranking (paper Section 10.4).

    Recording it in the ledger rather than in a mutable field on the block is what keeps blocks
    immutable. A block's identity cannot depend on how accessible someone later decided it should be, or
    demoting it would change its ``block_id`` and make it a different block.

    The paper leaves the decay function that governs demotion open (Section 12), so this records the
    decision and not a score: how much a demoted block is penalized, and whether the penalty fades, is
    a retrieval strategy the implementation owns.

    Attributes:
        record_type (Literal["demotion"]): Discriminator.
        block (BlockId): The block demoted.
        actor (Actor): Who demoted it.
        at (Timestamp): When.
        reason (str | None): Why.
        policy (str | None): Named policy that authorized it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["demotion"] = "demotion"
    block: BlockId
    actor: Actor
    at: Timestamp
    reason: str | None = None
    policy: str | None = None


class RemovalRecord(BaseModel):
    """
    Knowledge left the brain, and by which mechanism.

    Attributes:
        record_type (Literal["removal"]): Discriminator.
        blocks (list[BlockId]): What was removed.
        mechanism (RemovalMechanism): How it was removed.
        memory_type (MemoryType): Which module it was removed from.
        actor (Actor): Who removed it.
        at (Timestamp): When it was removed.
        reason (str): Why. Required: an unexplained removal is not auditable.
        policy (str | None): Named policy that authorized the removal.
        cascaded_from (BlockId | None): The block whose removal forced this one,
            when this record is part of a provenance cascade.
        resulting_roots (dict[str, MerkleRoot] | None): New Merkle root of each
            module the removal rewrote, keyed by memory type. A single logical
            removal of evidence can publish several new module versions at once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["removal"] = "removal"
    blocks: list[BlockId] = Field(min_length=1)
    mechanism: RemovalMechanism
    memory_type: MemoryType
    actor: Actor
    at: Timestamp
    reason: str = Field(min_length=1)
    policy: str | None = None
    cascaded_from: BlockId | None = None
    resulting_roots: dict[str, MerkleRoot] | None = None


class ValidationRecord(BaseModel):
    """
    A committed block received its verdict, and under which checks.

    Without this, "it was validated" is a claim a consumer takes from whoever committed. With it, the
    claim sits inside the signed composition and can be read back: which verdict, which checks produced
    it, and which task the proposal answered (paper Section 10.3).

    Attributes:
        record_type (Literal["validation"]): Discriminator.
        block (BlockId): The block the verdict admitted.
        verdict (ValidationStatus): What the gate decided. Only ``VALIDATED`` blocks are committed,
            so that is what a record accompanying a member says -- but the field is the full enum
            because recording the verdicts of candidates that were never committed is permitted.
        checks (list[str]): Identifiers of the checks that ran, sorted. The set that ran is what makes
            a verdict meaningful: the same ``VALIDATED`` under two different check sets is two
            different claims.
        actor (Actor): Who ran the gate.
        at (Timestamp): When it ran.
        task (str | None): The processing task the proposal answered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["validation"] = "validation"
    block: BlockId
    verdict: ValidationStatus
    checks: list[str] = Field(min_length=1)
    actor: Actor
    at: Timestamp
    task: str | None = None


ProvenanceEntry = Annotated[
    RegistrationRecord
    | DerivationRecord
    | NormalizationRecord
    | SupersessionRecord
    | DemotionRecord
    | ValidationRecord
    | RemovalRecord,
    Field(discriminator="record_type"),
]
"""One entry in the provenance ledger."""


class ProvenanceBlock(Block):
    """
    A single, immutable entry in the provenance ledger.

    Attributes:
        record (ProvenanceEntry): The recorded event.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.PROVENANCE

    record: ProvenanceEntry
