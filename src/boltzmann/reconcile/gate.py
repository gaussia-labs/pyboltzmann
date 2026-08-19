"""The semantic half of reconciliation: the result is validated as if it were an ingestion.

Because no textual conflict is representable, the conflicts that remain are semantic, and there are few
of them (paper Section 12.4). One proves the point. Branch A drops canonical block :math:`C`; branch B
adds a semantic block derived from :math:`C`. Each module's set arithmetic is individually correct -- the
drop is respected in the canonical composition, the addition in the semantic one -- and the result still
violates R1, because a derived block now cites evidence that is not in the composition. The invariant
that broke lives *between* modules, and Equation 1 never crosses that boundary.

So the structural reconciliation is automatic and its result is then judged:

    *A conflict in this protocol is a validation failure, not a differencing failure.*

Every question these conflicts raise -- does this evidence exist, does this contradict what is already
held, is this relation admissible, does the payload satisfy a registered schema -- is already asked on
the ingestion path, so no new checks are introduced here. The same
:class:`~boltzmann.ingest.validation.Validator` implementations run, against the composition the
reconciliation *would* produce, and the same four verdicts come out.

**What is judged, and what is not.** Only incoming blocks are judged: what was already ours passed this
gate when it was committed, and re-judging it would make a reconciliation able to reject knowledge nobody
proposed. Among the incoming, derived blocks go through the candidate-shaped gate, while canonical and
provenance blocks are checked structurally -- canonical registration is deterministic and provenance is
written by the protocol, so neither is anyone's proposal to accept or refuse on semantic grounds
(Section 7.1). They still have to decode and hash to the identity they are filed under.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import SupersessionRecord
from boltzmann.exceptions import BlockError, BlockSchemaError
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.proposer import Candidate
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES
from boltzmann.ingest.validation import ValidationIssue, ValidationStatus, Validator
from boltzmann.ingest.validators import CONTRADICTION_CODES, DEFAULT_VALIDATORS, REVIEW_CODES, DuplicateValidator
from boltzmann.merkle.tree import sorted_leaves
from boltzmann.module.module import Module

if TYPE_CHECKING:
    from collections.abc import Sequence

    from boltzmann.blocks.base import Block
    from boltzmann.module.composition import Composition
    from boltzmann.module.ledger import Ledger
    from boltzmann.store.base import BlockStore

INTEGRITY_CODE = "block-unreadable"
"""Issue code for an incoming block that does not decode, resolve, or hash to its identity."""

CANONICAL_VERSION_CODE = "schema-version-not-canonical"
"""Issue code for a block whose payload re-types to a different identity than the one it is filed under.

A version is a statement, not a preference: a block MUST be written under the oldest registered schema
its payload satisfies (paper Section 6.6). A block that was not is admissible nowhere, because two
conforming clients would compute two identities for the same knowledge.
"""

RECONCILE_VALIDATORS: tuple[Validator, ...] = tuple(
    check for check in DEFAULT_VALIDATORS if not isinstance(check, DuplicateValidator)
)
"""The checks a reconciliation applies to incoming derived blocks: the ingestion gate's, minus duplicates.

In an ingestion, "this block is already in the composition" means the proposal is a no-op that would still
advance a root. In a reconciliation it means nothing: every incoming block is a member of the result by
construction, because being a member of the result is exactly what is being judged. Keeping the check would
reject an entire contribution and report the reason as duplication.

Every other check asks about the state that will exist, which is the reconciled composition, and every one
of them applies unchanged.

A deployment adding a domain check should extend this rather than :data:`DEFAULT_VALIDATORS`, for the same
reason the ingestion gate documents: passing a sequence replaces the set rather than adding to it.
"""

PRECEDENCE_CODE = "competing-supersession"
"""Issue code for two histories that replaced the same block with different successors.

Both successors are admissible blocks and both supersession edges are legitimately recorded. What is
unresolved is which one takes precedence, and that is a question the protocol must not answer by itself: the
ledger would otherwise let whichever record it read last win in silence (paper Section 12.4).
"""

UNCITED_CODE = "evidence-not-found"
"""Issue code for a derived block whose citations cannot be established at all.

Deliberately the same code :class:`~boltzmann.ingest.validators.EvidenceValidator` raises. A derived
block with no evidence and one whose evidence is absent are the same failure of R1, and giving them
different codes would ask a caller to handle one condition twice.
"""


RECONCILE_REVIEW_CODES = REVIEW_CODES | {PRECEDENCE_CODE}
"""Issue codes that mean ``PENDING_REVIEW`` in a reconciliation.

The ingestion gate's set plus the one condition only a reconciliation can produce. Kept separate rather than
added to :data:`~boltzmann.ingest.validators.REVIEW_CODES`, because a competing supersession cannot arise on
the ingestion path -- there is only one history there.
"""


class MissingEvidence(StrEnum):
    """Why evidence a reconciled block cites is not in the composition (paper Section 12.5).

    The block is rejected either way, and it is the *diagnosis* that differs -- same verdict, opposite
    advice. Collapsing the two discards legitimate work over a packaging mistake and offers no way to
    tell which happened, so an implementation must distinguish them.
    """

    DROPPED_DELIBERATELY = "dropped_deliberately"
    """A removal record exists: this brain judged the evidence wrong and excluded it.

    The contribution rests on something that was deliberately removed, and resending it would re-import
    exactly what was excluded. The contributor should be told **not** to resend.
    """

    NEVER_HELD = "never_held"
    """No removal record, and the identity is unknown here: the evidence was never present.

    The contribution shipped a derived block without its canonical source. The work may be perfectly
    good and the transfer was incomplete, so the contributor should be told to resend it **whole**.
    """


class BlockVerdict(BaseModel):
    """
    The verdict on one incoming block.

    The unit is a block rather than a proposal, because an incoming block already has an identity: it was
    committed in the history it came from. The verdicts and the issue vocabulary are the ingestion gate's
    unchanged.

    Attributes:
        block (BlockId): Which block.
        memory_type (MemoryType): Which module it belongs to.
        status (ValidationStatus): The verdict. Only ``VALIDATED`` enters the reconciled composition.
        issues (list[ValidationIssue]): What the checks found.
        conflicts_with (list[BlockId]): The blocks this one is in conflict with -- what it contradicts, or
            the competing successors when the open question is precedence. Named rather than merely counted,
            because settling either kind of conflict means choosing among them.
        missing_evidence (dict[BlockId, MissingEvidence]): For each citation that is not in the
            reconciled composition, why it is absent. This is what turns one verdict into two pieces of
            advice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block: BlockId
    memory_type: MemoryType
    status: ValidationStatus
    issues: list[ValidationIssue] = Field(default_factory=list)
    conflicts_with: list[BlockId] = Field(default_factory=list)
    missing_evidence: dict[BlockId, MissingEvidence] = Field(default_factory=dict)

    @property
    def is_admissible(self) -> bool:
        """Whether this block may enter the reconciled composition."""
        return self.status is ValidationStatus.VALIDATED


class IncomingReport(BaseModel):
    """
    The verdict on everything a contribution brings.

    This is the report a maintainer acts on: it names which parts of a contribution fit before anything
    is decided, which is where this model departs usefully from reading a diff.

    Attributes:
        verdicts (list[BlockVerdict]): One entry per incoming block, in canonical module then leaf order.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[BlockVerdict] = Field(default_factory=list)

    def by_status(self, status: ValidationStatus) -> list[BlockVerdict]:
        """
        The verdicts with a given outcome.

        Args:
            status (ValidationStatus): Which outcome to select.

        Returns:
            list[BlockVerdict]: The matching verdicts.
        """
        return [verdict for verdict in self.verdicts if verdict.status is status]

    @property
    def admitted(self) -> list[BlockVerdict]:
        """The blocks that may enter the reconciled composition."""
        return [verdict for verdict in self.verdicts if verdict.is_admissible]

    @property
    def refused(self) -> dict[MemoryType, list[BlockId]]:
        """The blocks that must be excluded from the result, per module."""
        refused: dict[MemoryType, list[BlockId]] = {}
        for verdict in self.verdicts:
            if not verdict.is_admissible:
                refused.setdefault(verdict.memory_type, []).append(verdict.block)
        return refused

    @property
    def is_blocked(self) -> bool:
        """Whether any candidate is still ``PENDING_REVIEW``.

        Section 12.4 forbids committing a reconciliation while this holds: the protocol declined to
        decide, and committing would decide for it.
        """
        return any(verdict.status is ValidationStatus.PENDING_REVIEW for verdict in self.verdicts)

    @property
    def is_clean(self) -> bool:
        """Whether every incoming block was validated."""
        return all(verdict.is_admissible for verdict in self.verdicts)

    @property
    def advice(self) -> dict[BlockId, MissingEvidence]:
        """Every diagnosed absence, across all verdicts, keyed by the evidence that is missing."""
        return {cited: diagnosis for verdict in self.verdicts for cited, diagnosis in verdict.missing_evidence.items()}


def judge_incoming(
    incoming: dict[MemoryType, list[BlockId]],
    reconciled: dict[MemoryType, Composition],
    store: BlockStore,
    ledger: Ledger,
    validators: Sequence[Validator] | None = None,
) -> IncomingReport:
    """
    Judge the blocks a reconciliation would bring in, against the composition it would produce.

    Canonical and provenance blocks are judged first and the ones that fail are withdrawn from the view
    the derived blocks are checked against. Otherwise a semantic block could be admitted on the strength
    of evidence that was itself refused -- the same ordering an ingestion has, where a source is
    registered before anything cites it.

    Args:
        incoming (dict[MemoryType, list[BlockId]]): Per module, the blocks entering this brain.
        reconciled (dict[MemoryType, Composition]): The compositions Equation 1 produced.
        store (BlockStore): Where the blocks' bytes live.
        ledger (Ledger): The provenance view of the reconciled state. It supplies the citations of a
            derived block whose own schema does not carry them, and it is the removal ledger the
            missing-evidence diagnosis reads.
        validators (Sequence[Validator] | None): Checks to apply to derived blocks. Defaults to
            :data:`RECONCILE_VALIDATORS`. Passing a sequence replaces the set.

    Returns:
        IncomingReport: One verdict per incoming block.
    """
    modules = {memory_type: Module(memory_type, store, composition) for memory_type, composition in reconciled.items()}

    verdicts: list[BlockVerdict] = []
    withdrawn: set[BlockId] = set()

    for memory_type in MemoryType:
        if memory_type in PROPOSABLE_MEMORY_TYPES:
            continue
        for block_id in incoming.get(memory_type, []):
            verdict = _judge_preserved(block_id, memory_type, store, ledger)
            if not verdict.is_admissible:
                withdrawn.add(block_id)
            verdicts.append(verdict)

    if withdrawn:
        modules = {
            memory_type: Module(memory_type, store, _without(module.composition, withdrawn))
            for memory_type, module in modules.items()
        }

    checks = RECONCILE_VALIDATORS if validators is None else validators
    for memory_type in MemoryType:
        if memory_type not in PROPOSABLE_MEMORY_TYPES:
            continue
        for block_id in incoming.get(memory_type, []):
            verdicts.append(_judge_derived(block_id, memory_type, store, modules, ledger, checks))

    return IncomingReport(verdicts=verdicts)


def _without(composition: Composition, blocks: set[BlockId]) -> Composition:
    """A view of a composition with some blocks withheld, built directly to bypass the append-only rule.

    Not a drop: nothing is being removed from a brain. These blocks never entered, and an append-only
    module must not refuse to *decline* a block it was offered.
    """
    from boltzmann.module.composition import Composition as CompositionType

    return CompositionType(composition.memory_type, [block for block in composition.block_ids if block not in blocks])


def _judge_preserved(
    block_id: BlockId,
    memory_type: MemoryType,
    store: BlockStore,
    ledger: Ledger,
) -> BlockVerdict:
    """Check an incoming canonical or provenance block.

    Almost nothing semantic is asked. These two are the modules the protocol does not delegate: canonical
    registration preserves observed bytes and needs no interpretation, and provenance is the audit record the
    protocol writes itself. So what is checked is that the block is readable at the identity it arrived
    under -- a contribution cannot introduce evidence this brain cannot resolve.

    The one exception is a supersession record, and it is not a judgment about the record. Two histories may
    each have replaced the same block with something different, and the merged ledger then holds two answers
    to one precedence question. Both records are admissible and both edges stay recorded; what cannot happen
    is committing while the question is open.
    """
    issues = _readable(block_id, memory_type, store)
    contenders: list[BlockId] = []
    if not issues and memory_type is MemoryType.PROVENANCE:
        issues, contenders = _precedence(block_id, store, ledger)
    return BlockVerdict(
        block=block_id,
        memory_type=memory_type,
        status=_verdict(issues),
        issues=issues,
        conflicts_with=contenders,
    )


def _precedence(
    block_id: BlockId,
    store: BlockStore,
    ledger: Ledger,
) -> tuple[list[ValidationIssue], list[BlockId]]:
    """Whether an incoming supersession record leaves precedence undecided, and between which blocks.

    The contenders are returned rather than only described, because settling the question means naming a
    winner among them: a caller that had to recover them by reading the message could pick something that was
    never on offer.

    Asked of the ledger as *contested* rather than merely competing, so a question already settled by an
    earlier tie-break does not reopen. Both original edges stay recorded either way -- what changes is whether
    a precedence answer exists.
    """
    record = getattr(store.get_block(block_id), "record", None)
    if not isinstance(record, SupersessionRecord):
        return [], []
    contenders = ledger.contested(record.supersedes)
    if not contenders:
        return [], []

    ordered = sorted_leaves(contenders)
    named = ", ".join(block.short for block in ordered)
    issue = ValidationIssue(
        code=PRECEDENCE_CODE,
        detail=(
            f"{record.supersedes.short} is superseded by more than one block ({named}); both edges are "
            f"recorded and which one takes precedence is not the protocol's to decide"
        ),
    )
    return [issue], ordered


def _readable(block_id: BlockId, memory_type: MemoryType, store: BlockStore) -> list[ValidationIssue]:
    """Whether a block decodes, resolves, and hashes to the identity it is filed under."""
    try:
        block = store.get_block(block_id)
    except BlockError as error:
        return [ValidationIssue(code=INTEGRITY_CODE, detail=str(error))]

    if block.MEMORY_TYPE is not memory_type:
        return [
            ValidationIssue(
                code=INTEGRITY_CODE,
                detail=(
                    f"block {block_id.short} arrived in the {memory_type.value} module but its envelope "
                    f"declares {block.MEMORY_TYPE.value}"
                ),
            )
        ]
    return []


def _judge_derived(
    block_id: BlockId,
    memory_type: MemoryType,
    store: BlockStore,
    modules: dict[MemoryType, Module],
    ledger: Ledger,
    checks: Sequence[Validator],
) -> BlockVerdict:
    """Put one incoming derived block through the ingestion gate."""
    issues = _readable(block_id, memory_type, store)
    if issues:
        return BlockVerdict(block=block_id, memory_type=memory_type, status=ValidationStatus.REJECTED, issues=issues)

    block = store.get_block(block_id)
    citations = _citations(block, block_id, ledger)
    if not citations:
        return BlockVerdict(
            block=block_id,
            memory_type=memory_type,
            status=ValidationStatus.REJECTED,
            issues=[
                ValidationIssue(
                    code=UNCITED_CODE,
                    detail=(
                        f"block {block_id.short} cites no evidence, in its payload or in the provenance it "
                        f"arrived with, so there is no source it can be audited against"
                    ),
                    field="evidence",
                )
            ],
        )

    candidate = Candidate(
        memory_type=memory_type,
        payload=_payload(block),
        evidence=citations,
        locator=ledger.locators.get(block_id),
    )

    # The task states what the checks are allowed to assume about the request. A reconciliation has no
    # task -- nobody asked a model for anything -- so one is synthesized that permits exactly what
    # arrived, from the evidence it arrived citing. Fabricating a wider one would let a check pass a block
    # the incoming history had no licence to produce.
    task = _task_for(memory_type, citations[0])

    issues = [issue for check in checks for issue in check.check(candidate, task, modules)]
    identity = _identity_issue(candidate, block_id)
    if identity is not None:
        issues = [*issues, identity]

    return BlockVerdict(
        block=block_id,
        memory_type=memory_type,
        status=_verdict(issues),
        issues=issues,
        conflicts_with=_conflicts(candidate, modules, issues),
        missing_evidence=_diagnose(citations, modules, ledger),
    )


def _citations(block: Block, block_id: BlockId, ledger: Ledger) -> list[BlockId]:
    """What a derived block rests on, from the block itself or from the provenance it arrived with.

    A block is self-describing where its schema allows it, and where it does not -- the v1 schemas carry
    no ``evidence`` field -- the derivation record does. Reading both is not a fallback: the ledger is
    where the citation lives for a v1 block, and the payload is where it lives for a v2 one.
    """
    declared = getattr(block, "evidence", None)
    if declared:
        return list(declared)
    return list(ledger.evidence.get(block_id, []))


def _payload(block: Block) -> dict[str, Any]:
    """A block's payload as the checks expect to receive it, straight from the envelope."""
    return dict(block.envelope()["payload"])


def _task_for(memory_type: MemoryType, source: BlockId) -> Any:
    """A task describing what arrived, for the checks that read one."""
    from boltzmann.ingest.task import ProcessingTask, TaskOperation

    return ProcessingTask(
        operation=TaskOperation.EXTRACT_KNOWLEDGE,
        source=source,
        allowed_memory_types=[memory_type],
    )


def _identity_issue(candidate: Candidate, block_id: BlockId) -> ValidationIssue | None:
    """Whether re-typing the payload reproduces the identity the block arrived under.

    It has to. Both sides write a block under the oldest registered schema its payload satisfies, so a
    payload that re-types to a different identity was written under a schema its producer chose rather
    than one its payload required -- and that is precisely the divergence ``schema_version`` inside
    ``block_id`` was meant to make impossible.
    """
    from boltzmann.ingest.validators import build_block

    try:
        rebuilt = build_block(candidate)
    except (BlockSchemaError, ValueError):
        return None  # SchemaValidator already reported it; do not double-report.
    if rebuilt.block_id == block_id:
        return None
    return ValidationIssue(
        code=CANONICAL_VERSION_CODE,
        detail=(
            f"block {block_id.short} re-types to {rebuilt.block_id.short}: its payload satisfies an older "
            f"registered schema than the one it was written under, so two conforming clients would "
            f"compute two identities for the same knowledge"
        ),
    )


def _verdict(issues: list[ValidationIssue]) -> ValidationStatus:
    """Map issue codes onto a verdict, exactly as the ingestion gate does."""
    if not issues:
        return ValidationStatus.VALIDATED
    codes = {issue.code for issue in issues}
    if codes <= CONTRADICTION_CODES:
        return ValidationStatus.CONTRADICTED
    if codes <= CONTRADICTION_CODES | RECONCILE_REVIEW_CODES:
        return ValidationStatus.PENDING_REVIEW
    return ValidationStatus.REJECTED


def _conflicts(candidate: Candidate, modules: dict[MemoryType, Module], issues: list[ValidationIssue]) -> list[BlockId]:
    """The blocks a contradicted candidate disagrees with, named rather than merely counted."""
    from boltzmann.ingest.validators import conflicts_for

    if not {issue.code for issue in issues} & CONTRADICTION_CODES:
        return []
    return conflicts_for(candidate, modules)


def _diagnose(
    citations: list[BlockId],
    modules: dict[MemoryType, Module],
    ledger: Ledger,
) -> dict[BlockId, MissingEvidence]:
    """Why each absent citation is absent (paper Section 12.5).

    Provenance already holds the answer, because it is the removal ledger. A removal record means this
    brain judged the evidence wrong; no record and an unknown identity means the evidence was never here
    and the transfer was incomplete. Rejection is not final in either case: if a replacement source is
    later registered, ``rederive`` can rebuild what was lost against it. That is a separate, deliberate
    step, and it must not be folded into a reconciliation -- one that quietly re-derived rejected blocks
    against substituted evidence would be inventing knowledge in the middle of an operation the operator
    believes to be mechanical.
    """
    canonical = modules.get(MemoryType.CANONICAL)
    diagnosis = {}
    for cited in citations:
        if canonical is not None and cited in canonical:
            continue
        diagnosis[cited] = (
            MissingEvidence.DROPPED_DELIBERATELY if cited in ledger.removed else MissingEvidence.NEVER_HELD
        )
    return diagnosis
