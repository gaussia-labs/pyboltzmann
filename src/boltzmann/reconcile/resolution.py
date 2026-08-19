"""The state of a reconciliation someone is still resolving.

Section 12 says a conflict surfaces as a candidate in ``PENDING_REVIEW`` or ``CONTRADICTED`` rather than as
an unresolvable state, and that the verdicts are the report a maintainer acts on. It does not say how the
maintainer then acts, which is what this module is: the operator's half of the loop, modelled on version
control because that is the workflow the verdicts imply -- inspect what did not apply, decide it item by
item, then continue or abandon.

**Decisions are persisted; the plan is not.** A plan is a deterministic function of the local head, the other
history, the ancestor, and the blocks in the store, so it is recomputed on every ``continue`` rather than
stored. Storing verdicts would mean acting on a snapshot of a judgment that may no longer hold, and it would
put a large derived object in a mutable pointer. What is genuinely new information, and therefore worth
persisting, is what a person decided.

**Not every decision is available.** In version control you can force anything into a commit. Here some
invariants are structural rather than advisory: a derived block whose evidence is absent from the
composition breaks R1, nothing downstream would detect it -- ``verify`` checks hashes and compositions, not
citations across modules -- and so admitting one is not offered. The resolution for that is to fix the cause,
which is an ordinary commit and belongs outside the reconciliation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.provenance import Actor
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.identity.time import Timestamp, utc_timestamp
from boltzmann.reconcile.requests import ReconcilePlan, ReconcileStrategy

if TYPE_CHECKING:
    from boltzmann.ingest.validation import ValidationStatus


class ResolutionKind(StrEnum):
    """What a person decided about one block that did not apply cleanly."""

    ADMIT = "admit"
    """Let it in anyway.

    Available for a contradiction, which Section 12.4 treats as information rather than a defect -- holding
    two claims that disagree is a legitimate state, and which one is right is not a question the protocol
    answers. Also available for a candidate the protocol declined to decide.

    Never available for a rejection. That is the one place where this differs from version control on
    purpose.
    """

    REJECT = "reject"
    """Leave it out. Always available: declining a contribution needs no justification the protocol can check.

    The block is not destroyed. It stays in the store and in the history it came from; the new root simply
    does not name it.
    """

    PREFER = "prefer"
    """Settle a precedence question by naming the winner.

    For two histories that replaced the same block with different successors. Both successors are admissible
    and both supersession edges stay recorded -- what this decides is which one takes precedence, and it is
    recorded the only way this architecture can record precedence: as one more supersession edge, from the
    winner over the loser.
    """


class Resolution(BaseModel):
    """
    One decision, and who made it.

    Attributes:
        kind (ResolutionKind): What was decided.
        prefer (BlockId | None): The winning successor, for a ``PREFER``.
        actor (Actor): Who decided. Recorded for the same reason a removal records one: a reconciliation that
            admitted a contradiction on somebody's judgment is not auditable if the judgment is anonymous.
        reason (str | None): Why.
        at (Timestamp): When.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ResolutionKind
    prefer: BlockId | None = None
    actor: Actor
    reason: str | None = None
    at: Timestamp = Field(default_factory=utc_timestamp)


class ReconcileState(BaseModel):
    """
    A reconciliation in progress, as stored in the ``reconcile`` pointer.

    The second piece of mutable state a brain has, and the only one besides the head pointer. It is not part
    of any snapshot and never published: it describes an operation, not a version. The concluding commit is
    still a single head move, so atomicity is unchanged.

    Attributes:
        boltzmann (int): Protocol version that wrote this.
        theirs (OciDigest): The history being joined.
        ancestor (OciDigest): The snapshot the two parted from.
        strategy (ReconcileStrategy): The strategy that was chosen when it started. Recorded rather than
            asked again, because it is the attribution decision and re-asking would let it drift.
        actor (Actor): Who started it.
        reason (str): Why.
        head (OciDigest): The snapshot that was current when it started. If the brain has moved since, this
            state describes a reconciliation of something else.
        resolutions (dict[BlockId, Resolution]): What has been decided so far.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    theirs: OciDigest
    ancestor: OciDigest
    strategy: ReconcileStrategy
    actor: Actor
    reason: str = Field(min_length=1)
    head: OciDigest
    resolutions: dict[BlockId, Resolution] = Field(default_factory=dict)

    def with_resolution(self, block: BlockId, resolution: Resolution) -> ReconcileState:
        """
        Derive a state with one more decision recorded.

        Args:
            block (BlockId): The block decided.
            resolution (Resolution): The decision. Replaces an earlier one for the same block: changing your
                mind before concluding is not an error.

        Returns:
            ReconcileState: The updated state.
        """
        return self.model_copy(update={"resolutions": {**self.resolutions, block: resolution}})


class ReconcileStatus(BaseModel):
    """
    Where a reconciliation in progress stands.

    Attributes:
        state (ReconcileState): What was started and what has been decided.
        plan (ReconcilePlan): The plan, recomputed now rather than remembered.
        unresolved (list[BlockId]): Blocks that did not apply cleanly and have no decision yet. While this is
            non-empty the reconciliation cannot be concluded.
        resolved (list[BlockId]): Blocks that did not apply cleanly and now have one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ReconcileState
    plan: ReconcilePlan
    unresolved: list[BlockId] = Field(default_factory=list)
    resolved: list[BlockId] = Field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        """Whether every open question has an answer, and the reconciliation can be concluded."""
        return not self.unresolved

    def verdict_for(self, block: BlockId) -> ValidationStatus | None:
        """
        The verdict on one block, as the recomputed plan reports it.

        Args:
            block (BlockId): Which block.

        Returns:
            ValidationStatus | None: Its verdict, or ``None`` if the plan does not judge it.
        """
        for entry in self.plan.incoming.verdicts:
            if entry.block == block:
                return entry.status
        return None
