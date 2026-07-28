"""Types and the check interface for the validation gate (paper Section 8.3).

Results from the LLM do not enter automatically. The protocol requires that schema, references,
pages, types, duplicates, relations, and basic contradictions be checked; *which* checks a
deployment runs, and how it detects a contradiction, is the implementation's business. So this
module fixes the verdicts and the shape of a check, and leaves the checking to
:class:`Validator` implementations.

The four outcomes are not a severity scale. ``REJECTED`` means the proposal is malformed and can
never be committed; ``CONTRADICTED`` means it is well-formed but disagrees with knowledge already
held, which is a decision for a human or a policy rather than a defect; ``PENDING_REVIEW`` means
the protocol will not decide alone. Only ``VALIDATED`` reaches commit.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.provenance import Producer
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.proposer import Candidate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from boltzmann.blocks.memory_type import MemoryType
    from boltzmann.ingest.proposer import CandidateSet
    from boltzmann.ingest.task import ProcessingTask
    from boltzmann.module.module import Module


class ValidationStatus(StrEnum):
    """The verdict on one candidate (paper Section 8.3)."""

    VALIDATED = "validated"
    """Well-formed, referenced, and consistent. Eligible for commit."""

    PENDING_REVIEW = "pending_review"
    """Admissible but not decidable by the protocol alone."""

    REJECTED = "rejected"
    """Malformed, unreferenced, or duplicate. Never committed."""

    CONTRADICTED = "contradicted"
    """Well-formed but in conflict with knowledge already held."""


class ValidationIssue(BaseModel):
    """
    Why a candidate did not come out clean.

    Attributes:
        code (str): Stable identifier of the check that fired.
        detail (str): What was wrong, in a form a human can act on.
        field (str | None): Path within the candidate payload, when applicable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    field: str | None = None


class ValidatedCandidate(BaseModel):
    """
    A candidate with its verdict, and the typed block if it earned one.

    Attributes:
        candidate (Candidate): The original proposal.
        status (ValidationStatus): The verdict.
        block (Block | None): The typed block, present only when validated. This is the only
            place a proposal becomes a block.
        issues (list[ValidationIssue]): What the checks found.
        conflicts_with (list[BlockId]): Blocks this one contradicts, when the verdict is
            ``CONTRADICTED``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    candidate: Candidate
    status: ValidationStatus
    block: Block | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)
    conflicts_with: list[BlockId] = Field(default_factory=list)

    @property
    def is_committable(self) -> bool:
        """Whether this candidate may proceed to commit."""
        return self.status is ValidationStatus.VALIDATED and self.block is not None


class ValidationReport(BaseModel):
    """
    The verdict on a whole candidate set.

    Attributes:
        results (list[ValidatedCandidate]): One entry per proposal, in the order proposed.
        producer (Producer | None): Carried through from the candidate set, so the commit can record
            what produced each derived block without the caller having to restate it.
        task_id (str | None): The task these proposals answered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: list[ValidatedCandidate] = Field(default_factory=list)
    producer: Producer | None = None
    task_id: str | None = None

    def by_status(self, status: ValidationStatus) -> list[ValidatedCandidate]:
        """
        The results with a given verdict.

        Args:
            status (ValidationStatus): The verdict to select.

        Returns:
            list[ValidatedCandidate]: The matching results.
        """
        return [result for result in self.results if result.status is status]

    @property
    def committable(self) -> list[ValidatedCandidate]:
        """The results that may proceed to commit."""
        return [result for result in self.results if result.is_committable]

    @property
    def is_clean(self) -> bool:
        """Whether every proposal was validated."""
        return all(result.status is ValidationStatus.VALIDATED for result in self.results)


@runtime_checkable
class Validator(Protocol):
    """One check applied to a candidate. Implemented by the caller."""

    @property
    def code(self) -> str:
        """Stable identifier of this check, cited by the issues it raises."""
        ...

    def check(
        self,
        candidate: Candidate,
        task: ProcessingTask,
        modules: dict[MemoryType, Module],
    ) -> list[ValidationIssue]:
        """
        Inspect one candidate.

        Args:
            candidate (Candidate): The proposal to check.
            task (ProcessingTask): The task it answers, which states what was allowed.
            modules (dict[MemoryType, Module]): The installed modules, for duplicate and
                contradiction detection.

        Returns:
            list[ValidationIssue]: What was wrong. Empty means the check passed.
        """
        ...


def validate(
    candidates: CandidateSet,
    task: ProcessingTask,
    modules: dict[MemoryType, Module],
    validators: Sequence[Validator] | None = None,
) -> ValidationReport:
    """
    Run the validation gate over a candidate set.

    Every check runs against every candidate, rather than stopping at the first failure, because a
    caller fixing a proposal wants the whole list.

    Args:
        candidates (CandidateSet): What the external model proposed.
        task (ProcessingTask): The task the proposals answer.
        modules (dict[MemoryType, Module]): The installed modules.
        validators (Sequence[Validator] | None): Checks to apply. Defaults to the protocol's own set
            from :mod:`boltzmann.ingest.validators`; passing a sequence replaces it, so a deployment
            that wants domain checks in addition should include the defaults.

    Returns:
        ValidationReport: One verdict per proposal.
    """
    from boltzmann.ingest.validators import CONTRADICTION_CODES, DEFAULT_VALIDATORS, build_block

    checks = DEFAULT_VALIDATORS if validators is None else validators
    results = []

    for candidate in candidates.candidates:
        issues = [issue for check in checks for issue in check.check(candidate, task, modules)]
        codes = {issue.code for issue in issues}

        if not issues:
            results.append(
                ValidatedCandidate(
                    candidate=candidate,
                    status=ValidationStatus.VALIDATED,
                    block=build_block(candidate),
                )
            )
            continue

        # A contradiction on its own is not a defect; anything else is.
        status = ValidationStatus.CONTRADICTED if codes <= CONTRADICTION_CODES else ValidationStatus.REJECTED
        results.append(ValidatedCandidate(candidate=candidate, status=status, issues=issues))

    return ValidationReport(results=results, producer=candidates.producer, task_id=task.task_id)
