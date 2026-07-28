"""Episodic memory: what happened in a concrete context (paper Section 5).

Records events and experiences with time, context, participants, outcome, and
evidence. This is the only module that stays append-only: corrections generate new
episodes or supersession relations rather than drops that rewrite the past.
"""

from __future__ import annotations

from typing import ClassVar, Self

from pydantic import Field, model_validator

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId
from boltzmann.identity.time import Timestamp, parse_timestamp


class EpisodicBlock(Block):
    """
    A concrete experience, situated in time.

    Attributes:
        summary (str): What happened, in one statement.
        occurred_at (Timestamp): When the episode happened, in canonical UTC form.
        ended_at (Timestamp | None): When it finished, for episodes with duration.
        context (str | None): The setting the episode belongs to, such as a course,
            a project, or a conversation.
        participants (list[str] | None): Who took part.
        outcome (str | None): How it resolved.
        evidence (list[BlockId] | None): Canonical blocks that attest to the episode.
        tags (list[str] | None): Free-form labels for filtering.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.EPISODIC

    summary: str = Field(min_length=1)
    occurred_at: Timestamp
    ended_at: Timestamp | None = None
    context: str | None = None
    participants: list[str] | None = None
    outcome: str | None = None
    evidence: list[BlockId] | None = None
    tags: list[str] | None = None

    @model_validator(mode="after")
    def _check_interval(self) -> Self:
        """An episode cannot end before it started."""
        if self.ended_at is not None and parse_timestamp(self.ended_at) < parse_timestamp(self.occurred_at):
            raise ValueError(f"ended_at {self.ended_at} precedes occurred_at {self.occurred_at}")
        return self
