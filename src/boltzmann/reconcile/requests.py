"""Types for reconciling two histories (paper Section 12).

The operations are declared on :class:`~boltzmann.protocol.operations.BrainReconciliation`; this module
defines what each one takes and what it reports.

**The three strategies are recording strategies.** This inverts the intuition anyone brings from version
control. In Git the three differ both in how the result is computed and in how history is recorded: a
rebase replays patches, and can therefore land on a tree a merge would not have produced. Here a snapshot
is a complete statement of composition rather than a patch, so there is nothing to replay sequentially,
and all three produce the same set of blocks. What differs is only the lineage recorded, and therefore
who remains on record as the author.

So the choice is the operator's and it is not a matter of tidiness. Nothing in this module supplies a
default: :attr:`ReconcileRequest.strategy` is required, because once snapshots are signed the difference
between the three is attribution, and a default would be this SDK choosing whose name comes off the work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.authenticity.authenticator import Authorship
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import WritingActor
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.reconcile.gate import IncomingReport
from boltzmann.reconcile.merge import ModuleReconciliation


class ReconcileStrategy(StrEnum):
    """How a reconciliation records the history it joined."""

    MERGE = "merge"
    """Two or more parents. The other side's snapshots stay in the history, so what they signed still
    covers something and they remain on record as the author of their own work."""

    REBASE = "rebase"
    """One parent: mine. The other side's snapshots are replayed as new snapshots, which mints new
    identities and therefore invalidates any signature over the originals.

    Replaying immutable blocks onto a new base is deterministic, but it changes snapshot identities, which
    invalidates signatures over them and any root already published. That is exactly the property of a
    lineage rewrite, and the same rule applies: legitimate only before publication."""

    SQUASH = "squash"
    """One parent: mine, with the other side's snapshots collapsed into one.

    More useful here than in version control, because an ingestion session mints many intermediate
    snapshots nobody cares about individually. A squash compacts snapshot history and preserves every
    provenance record the collapsed snapshots produced -- see :class:`ReconcileResult`."""


class AttributionReport(BaseModel):
    """
    What choosing one strategy costs, stated rather than left to be discovered.

    All three strategies land the same blocks, so this is the whole difference between them. An
    implementation must report it as a consequence of the choice rather than let it happen silently
    (paper Section 12.3), and must not present a rebased or squashed contribution as bearing the
    contributor's signature.

    ``their_signatures_survive`` states a mechanical fact about detached signatures: they cover
    snapshot identities, so a strategy that keeps the other side's snapshots keeps their signatures
    covering something, and one that mints new identities leaves them covering nothing. It is
    reported now because the operator's decision is made now: the strategy is chosen before signatures
    exist to be invalidated, and a report that appeared only once authenticity shipped would arrive after
    the only moment it could have informed anything.

    Attributes:
        strategy (ReconcileStrategy): The strategy this describes.
        parents (int): How many parents the resulting snapshot names.
        snapshots_written (int): How many snapshots the operation mints.
        keeps_their_snapshots (bool): Whether the other side's snapshots remain in the resulting history.
        mints_new_identities (bool): Whether the operation creates new snapshot identities for work that
            already had one.
        their_signatures_survive (bool): Whether a signature the other side made still covers something in
            the resulting history. False means their work ends up attributed to whoever reconciled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: ReconcileStrategy
    parents: int = Field(ge=1)
    snapshots_written: int = Field(ge=1)
    keeps_their_snapshots: bool
    mints_new_identities: bool
    their_signatures_survive: bool


class ReconcileRequest(BaseModel):
    """
    An intent to join another history into this one.

    Attributes:
        theirs (OciDigest): The other history's head. It must already be held locally, which is what
            ``fetch`` is for.
        strategy (ReconcileStrategy): How to record the result. **Required**: the three differ in
            attribution, not in tidiness, so there is no defensible default.
        actor (Actor): Who is reconciling. Recorded for the same reason a removal records one -- an
            unattributed reconciliation is not auditable.
        reason (str): Why. Required, for the same reason.
        ancestor (OciDigest | None): The snapshot to reconcile against, when the caller knows it.
            Defaults to searching for the nearest one the two histories share.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    theirs: OciDigest
    strategy: ReconcileStrategy
    actor: WritingActor
    reason: str = Field(min_length=1)
    ancestor: OciDigest | None = None


class ReconcilePlan(BaseModel):
    """
    What a reconciliation would produce, computed before anything is written.

    The plan is strategy-independent, which is the point: the composition is identical under all three, so
    a plan can report the consequence of each and let the choice be made with that in hand rather than
    afterwards. It is also the report a maintainer acts on -- the incoming blocks arrive already judged, so
    which parts of a contribution fit is known before anything is decided, instead of being inferred by
    reading a diff.

    Attributes:
        ancestor (OciDigest): The snapshot the two histories parted from.
        theirs (OciDigest): The other history's head.
        modules (dict[MemoryType, ModuleReconciliation]): What Equation 1 produced, per module.
        incoming (IncomingReport): The verdict on every block entering this brain.
        attribution (dict[ReconcileStrategy, AttributionReport]): What each of the three would cost.
        cascaded (dict[MemoryType, list[BlockId]]): Blocks this brain holds that leave because the evidence
            they cite does. Equation 1 is set arithmetic over one module at a time, so a block excluded in
            the canonical module leaves its dependents behind in the semantic one -- individually correct,
            and a violation of R1 overall. The cascade a drop runs is what resolves it, and it is the same
            cascade (paper Sections 10.3 and 12.4).
        withdrawn (dict[MemoryType, list[BlockId]]): Everything leaving this brain's compositions, which is
            the exclusions of Equation 1 plus what they cascaded to. A reconciliation that removes work
            cannot be committed until someone says so, for the same reason one that cannot decide a
            candidate cannot: it would be a decision taken on the operator's behalf.
        collapsed (int): How many snapshots the other history added on top of the ancestor. This is what a
            squash collapses into one.
        replayable (int): How many of those versions can be reopened here, and therefore how many snapshots
            a rebase would write. Fewer than ``collapsed`` means the artifact carried only its head's
            compositions, so a rebase cannot preserve the granularity of the rest.
        untransferred (list[MemoryType]): Modules the other history names whose compositions never arrived.
            The reconciliation proceeds over what it can read, and what those modules would have
            contributed surfaces as verdicts on the blocks that cite them -- but a caller should be able to
            see that a transfer was incomplete without inferring it from a rejection.
        authorship (Authorship | None): Who signed the incoming head, judged as an *offered* proposal
            rather than as a head. This is where ``attributable`` becomes visible: a contributor whose
            key the trust root does not list is named rather than refused, which is how an open project
            hears from strangers. ``None`` when the incoming head carries no signature at all.
        carried (dict[MemoryType, ModuleRef]): Modules taken at their recorded root rather than reconciled,
            because neither side's composition is readable here. This is what lets a partial install
            publish back without dropping the modules it never fetched (paper Section 12.8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ancestor: OciDigest
    theirs: OciDigest
    modules: dict[MemoryType, ModuleReconciliation] = Field(default_factory=dict)
    incoming: IncomingReport = Field(default_factory=IncomingReport)
    cascaded: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    withdrawn: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    attribution: dict[ReconcileStrategy, AttributionReport] = Field(default_factory=dict)
    collapsed: int = Field(default=0, ge=0)
    replayable: int = Field(default=0, ge=0)
    untransferred: list[MemoryType] = Field(default_factory=list)
    authorship: Authorship | None = None
    carried: dict[MemoryType, ModuleRef] = Field(default_factory=dict)

    def members(
        self,
        memory_type: MemoryType,
        refused: Mapping[MemoryType, Iterable[BlockId]] | None = None,
    ) -> list[BlockId]:
        """
        One module's membership as a commit would write it.

        Equation 1's result, minus what the gate turned away and minus what the cascade took with the
        evidence it cited. Only blocks that emerge ``VALIDATED`` enter (paper Section 12.4).

        Args:
            memory_type (MemoryType): Which module.
            refused (Mapping[MemoryType, Iterable[BlockId]] | None): Which incoming blocks to withhold.
                Defaults to the gate's own refusals; a caller that has recorded decisions passes the
                adjusted set.

        Returns:
            list[BlockId]: The members, in canonical leaf order.
        """
        merged = self.modules.get(memory_type)
        if merged is None:
            return []
        turned_away = self.incoming.refused if refused is None else refused
        withheld = {*turned_away.get(memory_type, []), *self.cascaded.get(memory_type, [])}
        return [block_id for block_id in merged.block_ids if block_id not in withheld]

    def admitted(self, memory_type: MemoryType) -> list[BlockId]:
        """
        One module's membership under the gate's own verdicts, with no decisions applied.

        Args:
            memory_type (MemoryType): Which module.

        Returns:
            list[BlockId]: The members, in canonical leaf order.
        """
        return self.members(memory_type)

    @property
    def excluded(self) -> dict[MemoryType, list[BlockId]]:
        """Blocks leaving this brain's compositions, per module.

        A block one side dropped does not return because the other side still held it, so reconciliation
        can remove something this brain currently holds. The removal is auditable without a new record:
        the history that dropped it wrote a removal record, and that record is a provenance block, which
        Equation 1 folds in along with everything else.
        """
        return {memory_type: merged.removed for memory_type, merged in self.modules.items() if merged.removed}

    @property
    def is_blocked(self) -> bool:
        """Whether a candidate is still ``PENDING_REVIEW``, which forbids committing."""
        return self.incoming.is_blocked

    @property
    def is_clean(self) -> bool:
        """Whether this reconciliation can be carried out without anyone deciding anything.

        Every incoming block applied, and nothing this brain holds leaves.
        """
        return self.incoming.is_clean and not self.withdrawn

    @property
    def is_noop(self) -> bool:
        """Whether the other history is already contained in this one, so there is nothing to reconcile."""
        return all(merged.is_noop for merged in self.modules.values())


class ReconcileResult(BaseModel):
    """
    What a reconciliation committed.

    **No provenance record is written, and none is missing.** A reconciliation is recorded by the lineage
    itself: the snapshot names the histories it joined, which is the whole link. The removals it performs
    are recorded by the removal records the other history already wrote, which arrive as provenance blocks
    and are kept by Equation 1 like any other block. That is also why a squash preserves every provenance
    record the snapshots it collapsed produced: provenance is a module, so collapsing snapshots cannot
    drop one -- destroying the audit ledger to tidy a chain is precisely what Section 12.3 forbids, and
    here it is not expressible.

    Attributes:
        snapshot (Snapshot): The resulting state of the brain.
        strategy (ReconcileStrategy): Which strategy was chosen. Part of the record, not a detail of the
            call: it is what determines whose signature covers the result.
        attribution (AttributionReport): The consequence of that choice.
        parents (list[OciDigest]): The resulting snapshot's parents, first parent first.
        snapshots (list[OciDigest]): Every snapshot this operation wrote, oldest first. One for a merge or
            a squash, one per replayed snapshot for a rebase.
        roots (dict[MemoryType, MerkleRoot]): The new root of each module.
        admitted (dict[MemoryType, list[BlockId]]): What entered each module.
        excluded (dict[MemoryType, list[BlockId]]): What left each module.
        plan (ReconcilePlan): The plan that was carried out, including every verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: Snapshot
    strategy: ReconcileStrategy
    attribution: AttributionReport
    parents: list[OciDigest] = Field(default_factory=list)
    snapshots: list[OciDigest] = Field(default_factory=list)
    roots: dict[MemoryType, MerkleRoot] = Field(default_factory=dict)
    admitted: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    excluded: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)
    plan: ReconcilePlan
