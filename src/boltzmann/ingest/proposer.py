"""The boundary with the external LLM.

*The LLM proposes; the protocol governs what is stored* (paper Section 7.1). This
module is where that sentence becomes a type.

A proposer returns :class:`Candidate` objects, and a ``Candidate`` is deliberately
**not** a :class:`~boltzmann.blocks.base.Block`. It has no ``block_id``, because an
unvalidated proposal has no identity yet; it carries a raw ``payload`` mapping rather
than a typed model, because it has not been checked against a schema. The only way to
turn candidates into blocks is
:mod:`boltzmann.ingest.validation` followed by :mod:`boltzmann.ingest.commit`.

So the design rule of Section 7.1 -- the LLM never writes directly to the Merkle DAGs or
to the indices -- is not enforced by convention here. There is simply no method on this
interface that could.

The SDK ships **no** proposer implementation. Providing one would embed a model in the
protocol and break Principle 5. Adapting a provider is the caller's job, and satisfying
this interface is all it takes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Producer
from boltzmann.constants import CANDIDATES_SCHEMA
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.task import ProcessingTask


class Candidate(BaseModel):
    """
    An unvalidated proposal from an external model.

    Attributes:
        memory_type (MemoryType): Which kind of block is proposed.
        payload (dict[str, Any]): The proposed content, unchecked. Validation turns it
            into a typed block, or rejects it.
        evidence (list[BlockId]): Canonical blocks the proposal cites. Never empty: a
            derived block with no evidence has no root to be audited against.
        locator (str | None): Where in the source it came from -- a page, a line range,
            a timestamp.
        confidence (str | None): The model's own estimate, as a string because the
            protocol forbids floats inside anything that gets hashed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_type: MemoryType
    payload: dict[str, Any]
    evidence: list[BlockId] = Field(min_length=1)
    locator: str | None = None
    confidence: str | None = None


class CandidateSet(BaseModel):
    """
    A model's full response to one processing task: the ``boltzmann.candidates/v1`` schema.

    Attributes:
        schema_version (str): The schema this response claims to satisfy.
        task_id (str | None): The task this responds to.
        producer (Producer | None): What produced these proposals -- model and version, pipeline, or
            ingestion batch. The proposer is the only party that knows, and provenance needs it: a
            batch invalidation that drops "everything derived by model X version Y" can only work if
            the producer was recorded at commit time (paper Section 10.3).
        candidates (list[Candidate]): The proposals, in the order the model produced them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CANDIDATES_SCHEMA
    task_id: str | None = None
    producer: Producer | None = None
    candidates: list[Candidate] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.candidates)

    def of_type(self, memory_type: MemoryType) -> list[Candidate]:
        """
        The proposals of one memory type.

        A document may produce semantic and procedural memory without producing episodic
        memory; the classification depends on what the source actually contains
        (paper Section 8.2).

        Args:
            memory_type (MemoryType): Which kind to select.

        Returns:
            list[Candidate]: The matching proposals.
        """
        return [candidate for candidate in self.candidates if candidate.memory_type is memory_type]


@runtime_checkable
class CandidateProposer(Protocol):
    """Interprets a source and proposes typed blocks. Implemented by the caller, never here."""

    def __call__(self, task: ProcessingTask, source: bytes) -> CandidateSet:
        """
        Read a source and propose candidate blocks.

        Args:
            task (ProcessingTask): What the protocol is asking for, including the
                allowed memory types and the required references.
            source (bytes): The canonical bytes to interpret, or their normalized view.

        Returns:
            CandidateSet: The proposals, for the protocol to validate.
        """
        ...
