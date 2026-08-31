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

from collections.abc import Sequence
from enum import StrEnum
from typing import Annotated, ClassVar, Literal, Self, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

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


class Collaborator(Actor):
    """
    One party that took part in an operation without being the one who performed it.

    People and agents share one shape, so reading "who took part" never branches: the head is an
    actor's, and an agent adds the one thing a person does not have. A model, a runtime, a
    pipeline and a second person are all collaborators; what each *is* travels in ``kind``, never
    encoded into the identifier, because a reclassification must not be a rename.

    Attributes:
        model (str | None): The model this party ran, as an actor identifier. Named beside the
            party rather than as a separate list entry because the pair is the fact: the same
            model under a different harness is a different collaborator, since the harness decides
            what the model sees, how many turns it takes, and which tools it can reach. Keeping
            them together is what stays unambiguous when several agents write into one snapshot.

    No version string appears here, on purpose. A version is the field most likely to be invented
    by whoever fills the record in, it ages faster than everything beside it, and it buys less
    than the identity it would sit next to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None

    @model_validator(mode="after")
    def _a_person_runs_no_model(self) -> Self:
        """A human collaborator naming a model is a category error, and a silent one.

        Left unchecked it reads as "this person is a model" to every consumer that groups by
        ``model`` -- including a batch invalidation, which would then reach a person's work.
        """
        if self.kind is ActorKind.HUMAN and self.model is not None:
            raise ValueError(
                f"collaborator {self.id} is a person and names the model {self.model!r}; a person "
                f"does not run a model as part of their identity -- record the agent as its own "
                f"entry instead"
            )
        return self


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


# --- Schema version 2: who assisted ------------------------------------------------------------


class Attributed(BaseModel):
    """The member schema version 2 adds to every record: who took part.

    A mixin rather than seven copies of one field, and a *separate* set of record classes rather
    than a widening of the version-1 ones. The separation is forced and it is the right kind of
    forced: ``schema_version`` sits inside the envelope and therefore inside ``block_id``, so a
    record carrying a member version 1 never defined, while still claiming to be version 1, is
    exactly the lie the version field exists to prevent. A consumer that cannot read it must be
    able to *say* so, which it can only do if the version tells it.

    Attributes:
        assisted_by (list[Collaborator] | None): Everyone and everything that took part without
            being the actor. Absent rather than empty when nobody did, so a record that names no
            one keeps the bytes it would have had at version 1.

    Nothing here says who is responsible. There is an actor and there is whoever assisted, and the
    protocol takes no position on which of them answers for the knowledge -- a field claiming to
    settle that would be one every implementation had to interpret, and none of them would agree.
    """

    assisted_by: list[Collaborator] | None = None

    @model_validator(mode="after")
    def _identifiers_resolve(self) -> Self:
        """Every identifier in a version-2 record takes an accepted form.

        Strict here and permissive on :class:`Actor` itself, because version 2 is new: no record
        predates the rule, so nothing is stranded by enforcing it, and a legacy identifier
        reaching this far means a writer built a new record out of an old habit.
        """
        actor = getattr(self, "actor", None)
        if isinstance(actor, Actor):
            parse_actor_id(actor.id, field="actor id")
        for party in self.assisted_by or ():
            parse_actor_id(party.id, field="assisting party id")
            if party.model is not None:
                parse_actor_id(party.model, field="model id")
        return self


class RegistrationRecordV2(Attributed, RegistrationRecord):
    """A source was incorporated, and this is who took part."""


class NormalizationRecordV2(Attributed, NormalizationRecord):
    """A normalized view was produced, and this is who took part."""


class SupersessionRecordV2(Attributed, SupersessionRecord):
    """A block took precedence over an earlier one, and this is who took part."""


class DemotionRecordV2(Attributed, DemotionRecord):
    """A block's priority was lowered, and this is who took part."""


class ValidationRecordV2(Attributed, ValidationRecord):
    """A block received its verdict, and this is who took part."""


class DerivationRecordV2(Attributed):
    """
    A derived block was produced, and this is who took part.

    **A sibling of :class:`DerivationRecord`, not a subtype**, for the same reason
    :class:`~boltzmann.blocks.semantic.SemanticBlockV3` is a sibling: version 2 does not widen
    version 1, it *replaces* a member. ``producer`` named one thing where the answer is often
    several -- a person, an agent, a second person in the same session -- and it existed on this
    record alone, so a model that helped decide a supersession had nowhere to be recorded. The
    assisting parties answer the same question more completely and for every record kind.

    The two versions are therefore disjoint rather than nested, which is what makes selection
    unambiguous without consulting anything outside the payload. A payload carrying ``producer``
    satisfies version 1 and not version 2; one carrying ``assisted_by`` satisfies version 2 and
    not version 1; one carrying both, or neither, satisfies no registered schema and is refused.

    ``assisted_by`` is required here and optional everywhere else, deliberately. Version 1 obliged
    a writer to say what produced a derived block, and dropping ``producer`` must not quietly
    relax that into "derived, and I decline to say by what".

    Attributes:
        record_type (Literal["derivation"]): Discriminator.
        block (BlockId): The derived block.
        derived_from (list[BlockId]): The evidence it cites. Never empty: a derived block with no
            evidence has no root to be audited against.
        actor (Actor): Who ran the production.
        at (Timestamp): When it was produced.
        assisted_by (list[Collaborator]): Everything and everyone that took part. Required.
        task (str | None): Identifier of the processing task that yielded it.
        locator (str | None): Where in the source it came from, such as a page or a line range.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_type: Literal["derivation"] = "derivation"
    block: BlockId
    derived_from: list[BlockId] = Field(min_length=1)
    actor: Actor
    at: Timestamp
    assisted_by: list[Collaborator] = Field(min_length=1)
    task: str | None = None
    locator: str | None = None


ProvenanceEntryV2 = Annotated[
    RegistrationRecordV2
    | DerivationRecordV2
    | NormalizationRecordV2
    | SupersessionRecordV2
    | DemotionRecordV2
    | ValidationRecordV2,
    Field(discriminator="record_type"),
]
"""One entry in the provenance ledger, at schema version 2.

**Removal is deliberately absent.** Every other record is informational to a verifier; a removal
record is the one it must *decode* to decide a blocking question, because the removal invariant
asks whether every block absent from a composition has a reachable record explaining it. A client
without the version-2 schema cannot read the record, silently treats the removal as unexplained,
and rejects a perfectly valid brain for violating an invariant it in fact satisfies -- a specific,
confident, wrong accusation, and one the protocol gives no way to withdraw. Not being able to read
something must never be reported as that thing being wrong.

So a removal stays at version 1 forever, and records its actor as it always has. What it gives up
is naming who assisted in a removal; what it keeps is that every client can still check the ledger,
which is the property the ledger exists for.
"""


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


class ProvenanceBlockV2(Block):
    """
    A provenance entry that names who assisted.

    Selected by oldest-that-fits, never chosen: a record that names nobody validates as version 1
    and keeps the bytes -- and therefore the ``block_id`` -- it would have had before this version
    existed. Only a record that actually uses what version 2 added pays for version 2, which is
    the property that keeps a brain readable by clients that have not upgraded.

    Attributes:
        record (ProvenanceEntryV2): The recorded event.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.PROVENANCE
    SCHEMA_VERSION: ClassVar[int] = 2

    record: ProvenanceEntryV2


_VERSION_TWO_OF: dict[type[BaseModel], type[BaseModel]] = {
    RegistrationRecord: RegistrationRecordV2,
    DerivationRecord: DerivationRecordV2,
    NormalizationRecord: NormalizationRecordV2,
    SupersessionRecord: SupersessionRecordV2,
    DemotionRecord: DemotionRecordV2,
    ValidationRecord: ValidationRecordV2,
}
"""Which version-2 record answers the same question as each version-1 one."""


def provenance_block(
    record: ProvenanceEntry,
    assisted_by: Sequence[Collaborator] | None = None,
) -> ProvenanceBlock | ProvenanceBlockV2:
    """
    The provenance block for a record, under the oldest schema that can express it.

    A writer states the facts and never picks a version, which is the rule
    :meth:`~boltzmann.blocks.base.Block.build` exists to enforce: a version is a statement about
    which members a payload uses, and the payload already answers it. Naming nobody keeps the
    record at version 1 with the bytes -- and the ``block_id`` -- it would have had before version
    2 existed, so a brain only stops being readable by an older client at the point where it
    genuinely uses what version 2 added.

    A derivation is the one record whose two versions are disjoint rather than nested: version 2
    has no ``producer``, because the assisting parties answer the same question more completely.
    The producer's *version* string has no home there, and that narrowing is deliberate -- a
    version is the member most likely to be invented by whoever filled the record in.

    Args:
        record (ProvenanceEntry): The event, as a version-1 record.
        assisted_by (Sequence[Collaborator] | None): Who took part besides the actor. ``None`` or
            empty keeps the record at version 1.

    Returns:
        ProvenanceBlock | ProvenanceBlockV2: The block, under the version its content requires.
    """
    if not assisted_by or isinstance(record, RemovalRecord):
        # A removal never upgrades. See ProvenanceEntryV2: it is the one record a verifier must
        # decode to decide a blocking question, so a version an older client lacks would turn
        # "I cannot read this" into "you violated the invariant".
        return ProvenanceBlock(record=record)

    fields = record.model_dump(exclude_none=True)
    # Version 2 replaces it rather than carrying it; the model that produced the bytes is named
    # among the assisting parties, where the harness it ran inside is named beside it.
    fields.pop("producer", None)
    upgraded = _VERSION_TWO_OF[type(record)](**fields, assisted_by=list(assisted_by))
    return ProvenanceBlockV2(record=cast("ProvenanceEntryV2", upgraded))
