"""Types for canonical registration (paper Section 8.1).

Canonical evidence enters through three operations, and none of them edits a source in place:
a new edition is always a new block. The operations themselves are declared on
:class:`~boltzmann.protocol.operations.BrainWriter`; this module defines what they take and
what they return.

**Register.** Validate the format, compute the SHA-256, detect exact duplicates, store the
original as an immutable canonical block, optionally alongside a normalized view, record who
incorporated it and under what policy, and advance the canonical root. Registering a source
does not declare it true.

**Replace.** Register the new original as a distinct block and record a supersession edge to
the previous one. The caller chooses whether the superseded original stays in the composition
for audit or is dropped in the same commit. Register plus an edge, never a mutation of bytes.

**Drop.** The same drop declared on the retention path, with a privileged cascade. Allowed
only under explicit policy, because it forfeits re-derivation from that source (Principle 2).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.provenance import Actor
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.commit import CommitResult


class RegistrationRequest(BaseModel):
    """
    A source offered to a brain.

    Attributes:
        media_type (str): IANA media type of the bytes.
        actor (Actor): Who is incorporating the source.
        origin (str | None): Where it came from.
        license (str | None): License it is held under.
        retention_policy (str | None): Named policy that governs it.
        normalize_with (str | None): Name of a deterministic pipeline to produce a normalized
            view. Its identity is recorded in provenance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    media_type: str = Field(min_length=1)
    actor: Actor
    origin: str | None = None
    license: str | None = None
    retention_policy: str | None = None
    normalize_with: str | None = None


class RegistrationResult(BaseModel):
    """
    What a registration produced.

    Attributes:
        block_id (BlockId): Identity of the canonical block.
        commit (CommitResult | None): The commit that advanced the roots. ``None`` when the
            registration was a no-op.
        duplicate (bool): Whether these exact bytes were already registered. A duplicate is
            not an error: identical content has one identity, so there is nothing new to store
            and no new snapshot to publish.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: BlockId
    commit: CommitResult | None = None
    duplicate: bool = False
