"""The processing task: what the protocol asks an external LLM to do.

After canonical registration, the protocol returns a structured task identifying the
source, the instructions, the allowed memory types, the required references, and the
exact output schema (paper Section 8.2):

.. code-block:: json

    {
      "operation": "extract_knowledge",
      "source": "sha256:PDF123",
      "allowed_memory_types": ["episodic", "semantic", "procedural"],
      "requirements": ["cite source ranges", "do not invent"],
      "output_schema": "boltzmann.candidates/v1"
    }

This is a wire format, so it is implemented here rather than left to an
implementation: two clients that disagree on its shape cannot hand work to the same
model.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import CANDIDATES_SCHEMA
from boltzmann.identity.digest import BlockId

PROPOSABLE_MEMORY_TYPES = frozenset({MemoryType.EPISODIC, MemoryType.SEMANTIC, MemoryType.PROCEDURAL})
"""The only memory types an external LLM may propose blocks for.

Canonical registration is deterministic -- it preserves observed bytes and needs no
interpretation -- and provenance is written by the protocol itself. Leaving either open
to a proposer would put the LLM in charge of evidence or of the audit record, which is
precisely the boundary Section 7.1 draws.
"""


class TaskOperation(StrEnum):
    """What the protocol is asking the model to do."""

    EXTRACT_KNOWLEDGE = "extract_knowledge"
    """Read a registered source and propose typed blocks from it."""

    REDERIVE = "rederive"
    """Rebuild derived blocks against a replacement canonical source."""


class ProcessingTask(BaseModel):
    """
    A unit of interpretation delegated to an external, interchangeable model.

    Attributes:
        operation (TaskOperation): What to do.
        source (BlockId): The canonical block to interpret.
        allowed_memory_types (list[MemoryType]): Which kinds of block may be proposed.
        requirements (list[str]): Constraints the proposal must respect, such as
            citing source ranges or not inventing content.
        output_schema (str): Schema the response must satisfy.
        task_id (str | None): Identifier the resulting provenance records cite, so a
            batch invalidation can later name this task.
        instructions (str | None): Free-form guidance beyond the requirements.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: TaskOperation
    source: BlockId
    allowed_memory_types: list[MemoryType] = Field(min_length=1)
    requirements: list[str] = Field(default_factory=list)
    output_schema: str = CANDIDATES_SCHEMA
    task_id: str | None = None
    instructions: str | None = None

    @model_validator(mode="after")
    def _check_allowed_memory_types(self) -> Self:
        """A task may not invite the model to write evidence or the audit record."""
        forbidden = set(self.allowed_memory_types) - PROPOSABLE_MEMORY_TYPES
        if forbidden:
            names = ", ".join(sorted(kind.value for kind in forbidden))
            raise ValueError(
                f"a processing task cannot allow {names}: canonical evidence is registered "
                f"deterministically and provenance is written by the protocol, so neither is the "
                f"external model's to propose"
            )
        return self
