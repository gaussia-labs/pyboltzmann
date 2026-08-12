"""Procedural memory: how a task is performed (paper Section 5).

Represents action sequences, conditions, decisions, alternative paths, and success
criteria -- for example how to obtain the Fourier coefficients of a given function.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.content import NamesContent
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId


class Step(BaseModel):
    """
    One action in a procedure.

    Attributes:
        action (str): What to do.
        condition (str | None): When this step applies, for branching procedures.
        alternatives (list[str] | None): Other ways to accomplish the same step.
        uses (list[BlockId] | None): Semantic blocks the step relies on, such as the
            formula it applies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str = Field(min_length=1)
    condition: str | None = None
    alternatives: list[str] | None = None
    uses: list[BlockId] | None = None


class ProceduralBlock(Block):
    """
    A way of performing a task.

    Attributes:
        label (str): Short name of the procedure.
        goal (str): What the procedure accomplishes.
        steps (list[Step]): The ordered actions. Order is significant, so it is part
            of the block's identity.
        preconditions (list[str] | None): What must hold before starting.
        success_criteria (list[str] | None): How to tell the procedure worked.
        subject (str | None): Domain the procedure belongs to, for filtering.
        evidence (list[BlockId] | None): Canonical blocks this procedure cites.
            A canonical drop cascades to every block that lists it here.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.PROCEDURAL

    label: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)
    preconditions: list[str] | None = None
    success_criteria: list[str] | None = None
    subject: str | None = None
    evidence: list[BlockId] | None = None


class ProceduralBlockV2(NamesContent, ProceduralBlock):
    """
    A procedure that may name an artifact the steps produce or consume.

    A worked example, a reference output to diff against, a template the procedure fills in:
    the procedure's own datum. ``steps`` stays the procedure -- content does not replace the
    ordered actions, which are what makes this knowledge rather than an attachment.
    """

    SCHEMA_VERSION: ClassVar[int] = 2
