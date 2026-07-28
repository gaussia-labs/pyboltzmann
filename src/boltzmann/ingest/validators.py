"""The checks the protocol itself performs (paper Section 8.3).

The protocol checks schema, references, pages, types, duplicates, relations, and basic
contradictions. Those are mechanical: they need no judgment about the *content* of a proposal, only
about its shape and its relation to what is already held. So they live here.

What is **not** here is any check about whether the knowledge is good, useful, or worth keeping.
That is the external model's business, and a deployment that wants domain checks adds its own
:class:`~boltzmann.ingest.validation.Validator`.

The distinction between verdicts matters. A malformed or duplicate proposal is ``REJECTED`` and can
never be committed. A well-formed proposal that disagrees with knowledge already held is
``CONTRADICTED``, which is a decision for a human or a policy rather than a defect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import SemanticBlock
from boltzmann.exceptions import BlockSchemaError
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.validation import ValidationIssue

if TYPE_CHECKING:
    from boltzmann.identity.digest import BlockId
    from boltzmann.ingest.proposer import Candidate
    from boltzmann.ingest.task import ProcessingTask
    from boltzmann.module.module import Module


def build_block(candidate: Candidate) -> Block:
    """
    Turn a candidate's raw payload into a typed block.

    The candidate's citations are written into the block when its schema has an ``evidence`` field and the
    payload left it out. A block has to be self-describing: a consumer who installed only the semantic
    module has no provenance ledger to consult, so if the citation lived only in the ledger that consumer
    would hold knowledge with no way to see what it rests on. ``Candidate.evidence`` is the single truth
    -- a payload that states different citations is a validation failure, not a merge.

    Args:
        candidate (Candidate): The proposal to type.

    Returns:
        Block: The typed block.

    Raises:
        BlockSchemaError: If no schema is registered for the proposed memory type, or the payload does not
            satisfy it.
    """
    registry = Block.registry()
    versions = sorted(version for kind, version in registry if kind is candidate.memory_type)
    if not versions:
        raise BlockSchemaError(f"no schema registered for {candidate.memory_type.value} blocks")
    block_class = registry[(candidate.memory_type, versions[-1])]

    payload = dict(candidate.payload)
    if "evidence" in block_class.model_fields and payload.get("evidence") is None:
        payload["evidence"] = [str(cited) for cited in candidate.evidence]
    return block_class.model_validate(payload)


class EvidenceConsistencyValidator:
    """A payload that states its own citations must state the same ones the candidate cites."""

    code: ClassVar[str] = "evidence-mismatch"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Compare the payload's citations against the candidate's.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): Unused.

        Returns:
            list[ValidationIssue]: One issue if the two disagree.
        """
        stated = candidate.payload.get("evidence")
        if stated is None:
            return []
        if {str(value) for value in stated} == {str(cited) for cited in candidate.evidence}:
            return []
        return [
            ValidationIssue(
                code=self.code,
                detail=(
                    "the payload states different evidence than the candidate cites; the citation is "
                    "Candidate.evidence, so the two cannot disagree"
                ),
                field="evidence",
            )
        ]


class AllowedTypeValidator:
    """The proposal must be of a memory type the task invited."""

    code: ClassVar[str] = "memory-type-not-allowed"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Check the proposal's memory type against the task.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): The task it answers.
            modules (dict[MemoryType, Module]): Unused.

        Returns:
            list[ValidationIssue]: One issue if the type was not invited.
        """
        if candidate.memory_type in task.allowed_memory_types:
            return []
        allowed = ", ".join(sorted(kind.value for kind in task.allowed_memory_types))
        return [
            ValidationIssue(
                code=self.code,
                detail=f"proposed a {candidate.memory_type.value} block; the task allows: {allowed}",
                field="memory_type",
            )
        ]


class SchemaValidator:
    """The payload must satisfy the schema for its memory type."""

    code: ClassVar[str] = "schema"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Check that the payload types.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): Unused.

        Returns:
            list[ValidationIssue]: One issue per schema failure.
        """
        try:
            build_block(candidate)
        except BlockSchemaError as error:
            return [ValidationIssue(code=self.code, detail=str(error))]
        except ValueError as error:
            return [ValidationIssue(code=self.code, detail=f"payload does not satisfy the schema: {error}")]
        return []


class EvidenceValidator:
    """Every cited piece of evidence must exist in the canonical composition.

    A derived block whose evidence is not in the brain cannot be audited against its source, which
    is the whole point of Section 5's claim that canonical memory is the root of re-derivation.
    """

    code: ClassVar[str] = "evidence-not-found"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Check that the cited evidence is installed.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): The installed modules.

        Returns:
            list[ValidationIssue]: One issue per unresolvable citation.
        """
        canonical = modules.get(MemoryType.CANONICAL)
        if canonical is None:
            return [
                ValidationIssue(
                    code=self.code,
                    detail="the canonical module is not installed, so no citation can be checked",
                    field="evidence",
                )
            ]
        return [
            ValidationIssue(
                code=self.code,
                detail=f"cited evidence {cited.short} is not in the canonical composition",
                field="evidence",
            )
            for cited in candidate.evidence
            if cited not in canonical
        ]


class DuplicateValidator:
    """A block already in the target composition is not proposed again.

    Content addressing means an identical proposal has an identical identity, so committing it would
    be a no-op that still advanced a root.
    """

    code: ClassVar[str] = "duplicate"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Check whether the resulting block is already held.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): The installed modules.

        Returns:
            list[ValidationIssue]: One issue if the block is already in the composition.
        """
        module = modules.get(candidate.memory_type)
        if module is None:
            return []
        try:
            block = build_block(candidate)
        except (BlockSchemaError, ValueError):
            return []  # SchemaValidator reports this; do not double-report.
        if block.block_id in module:
            return [
                ValidationIssue(
                    code=self.code,
                    detail=f"block {block.block_id.short} is already in the {candidate.memory_type.value} composition",
                )
            ]
        return []


class RelationValidator:
    """A declared relation must point at a block the snapshot holds.

    Relations on a block are what the graph index is rebuilt from, so an edge to a block that is not
    installed would produce an index with a dangling target.
    """

    code: ClassVar[str] = "relation-target-not-found"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Check that relation targets resolve.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): The installed modules.

        Returns:
            list[ValidationIssue]: One issue per dangling target.
        """
        try:
            block = build_block(candidate)
        except (BlockSchemaError, ValueError):
            return []
        if not isinstance(block, SemanticBlock) or not block.relations:
            return []

        installed: set[BlockId] = set()
        for module in modules.values():
            installed.update(module.block_ids)

        return [
            ValidationIssue(
                code=self.code,
                detail=f"relation {relation.predicate!r} points at {relation.target.short}, which is not installed",
                field="relations",
            )
            for relation in block.relations
            if relation.target not in installed
        ]


class ContradictionValidator:
    """Basic contradiction detection: the same claim stated two different ways.

    Deliberately shallow. Two semantic blocks that share a label and a subject but state different
    things are flagged, because that is mechanically checkable. Anything deeper -- whether two
    differently worded statements actually disagree -- is a semantic judgment, and the protocol does
    not make semantic judgments.

    The verdict is ``CONTRADICTED``, not ``REJECTED``: a contradiction is information, and what to do
    with it is a policy decision.
    """

    code: ClassVar[str] = "contradiction"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Look for an existing block that makes the same claim differently.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): The installed modules.

        Returns:
            list[ValidationIssue]: One issue per conflicting block found, each naming it.
        """
        module = modules.get(MemoryType.SEMANTIC)
        if module is None or candidate.memory_type is not MemoryType.SEMANTIC:
            return []
        try:
            proposed = build_block(candidate)
        except (BlockSchemaError, ValueError):
            return []
        if not isinstance(proposed, SemanticBlock):
            return []

        issues = []
        for block_id in module.block_ids:
            if not module.store.is_resolvable(block_id):
                continue
            held = module.get(block_id)
            if not isinstance(held, SemanticBlock):
                continue
            if (
                held.label == proposed.label
                and held.subject == proposed.subject
                and held.kind is proposed.kind
                and held.statement != proposed.statement
            ):
                issues.append(
                    ValidationIssue(
                        code=self.code,
                        detail=(
                            f"block {block_id.short} already states {held.label!r} as "
                            f"{held.statement!r}, which differs from the proposal"
                        ),
                        field="statement",
                    )
                )
        return issues


def conflicts_for(candidate: Candidate, modules: dict[MemoryType, Module]) -> list[BlockId]:
    """
    The held blocks a proposal contradicts, so a reviewer can see both sides.

    A ``CONTRADICTED`` verdict that named no counterpart would tell a human that something disagrees
    without saying with what, which is not enough to decide.

    Args:
        candidate (Candidate): The proposal.
        modules (dict[MemoryType, Module]): The installed modules.

    Returns:
        list[BlockId]: The conflicting blocks, in a stable order.
    """
    module = modules.get(MemoryType.SEMANTIC)
    if module is None or candidate.memory_type is not MemoryType.SEMANTIC:
        return []
    try:
        proposed = build_block(candidate)
    except (BlockSchemaError, ValueError):
        return []
    if not isinstance(proposed, SemanticBlock):
        return []

    return sorted(
        (
            block_id
            for block_id in module.block_ids
            if module.store.is_resolvable(block_id) and _same_claim(module.get(block_id), proposed)
        ),
        key=lambda value: value.hex,
    )


def _same_claim(held: object, proposed: SemanticBlock) -> bool:
    """Whether two semantic blocks make the same claim in different words."""
    return (
        isinstance(held, SemanticBlock)
        and held.label == proposed.label
        and held.subject == proposed.subject
        and held.kind is proposed.kind
        and held.statement != proposed.statement
    )


class UndecidedValidator:
    """A check that declines to decide, which is not the same as deciding against.

    The protocol's own checks all decide, so nothing here produces this on its own. It exists because
    ``PENDING_REVIEW`` has to be reachable: a deployment whose domain check cannot settle a proposal --
    a claim needing a subject-matter expert, a licence question for a lawyer -- raises an issue with this
    code, and the gate reports the proposal as awaiting a decision rather than rejected.

    Subclass it, or simply emit an issue whose code is in :data:`REVIEW_CODES`.
    """

    code: ClassVar[str] = "pending-review"

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Decline to decide.

        Args:
            candidate (Candidate): The proposal.
            task (ProcessingTask): Unused.
            modules (dict[MemoryType, Module]): Unused.

        Returns:
            list[ValidationIssue]: One issue marking the proposal as awaiting a decision.
        """
        return [
            ValidationIssue(
                code=self.code,
                detail="this check cannot settle the proposal; a human or a policy has to",
            )
        ]


DEFAULT_VALIDATORS = (
    AllowedTypeValidator(),
    SchemaValidator(),
    EvidenceConsistencyValidator(),
    EvidenceValidator(),
    DuplicateValidator(),
    RelationValidator(),
    ContradictionValidator(),
)
"""The checks Section 8.3 assigns to the protocol, in the order they are cheapest to fail."""

CONTRADICTION_CODES = frozenset({ContradictionValidator.code})
"""Issue codes that mean ``CONTRADICTED`` rather than ``REJECTED``."""

REVIEW_CODES = frozenset({UndecidedValidator.code})
"""Issue codes that mean ``PENDING_REVIEW``: the check declined to decide, rather than deciding against."""
