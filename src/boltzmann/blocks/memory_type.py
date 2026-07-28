"""The five memory modules (paper Section 5).

Each module is named by the question it answers:

============  ==========================================================
canonical     What material was observed?
episodic      What happened in a concrete context?
semantic      What general knowledge was consolidated?
procedural    How is a task performed?
provenance    Where did it come from and how was it transformed?
============  ==========================================================

The enum lives in the kernel rather than in the module layer because a block's
memory type sits inside the hashed envelope: it is part of that block's identity,
not a property of where the block happens to be stored.
"""

from enum import StrEnum


class MemoryType(StrEnum):
    """A memory module, and therefore the type of a knowledge block."""

    CANONICAL = "canonical"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PROVENANCE = "provenance"

    @property
    def is_append_only(self) -> bool:
        """
        Whether the module refuses ``drop``.

        The episodic module is a chronological record of what happened, so
        corrections append new episodes or supersession relations rather than
        rewriting the past (paper Section 10.3).
        """
        return self is MemoryType.EPISODIC

    @property
    def is_droppable(self) -> bool:
        """Whether blocks may be excluded from this module's composition."""
        return not self.is_append_only

    @property
    def is_derived(self) -> bool:
        """
        Whether blocks of this type are interpretations that cite canonical evidence.

        Derived blocks are what a canonical drop cascades to (paper Section 10.3).
        """
        return self in {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}
