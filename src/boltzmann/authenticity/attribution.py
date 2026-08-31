"""Which claimed actors a signature actually vouches for (paper Section 8.9).

A provenance record names who performed an operation, and until a key stands behind that name it
is a *declared* identifier: whoever can write to a brain can write any name into its audit trail.
The trust root's ``subject`` is what makes the claim checkable, and this is where the two are put
side by side -- the actors a snapshot's new records name, against the subjects of the keys that
signed it.

**Reported, never enforced.** A snapshot legitimately carries records whose actors never signed it:
every merge does, since reconciliation brings another party's records into a history the local key
signs. A verifier that refused an unvouched actor would refuse the ordinary operation of the
divergence chapter. What it must not do is pass silently -- an actor nobody vouches for is a
declared name, exactly what it was before subjects existed, and saying so is the whole value of
having asked.

Assisting parties are never compared. Nothing expects a model to have signed anything, and treating
their absence from the key list as a finding would bury the one comparison that means something.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ProvenanceBlock, ProvenanceBlockV2
from boltzmann.exceptions import BoltzmannError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.identity.principal import is_actor_id
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.store.base import BlockStore


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """How far a snapshot's signatures reach into what its records claim.

    Three outcomes rather than two, because "not vouched for" and "could not be compared at all"
    call for different responses, and a report that collapsed them would leave a reader unable to
    tell a stranger's name from an old one.

    Attributes:
        snapshot (OciDigest): What was examined.
        verified (tuple[str, ...]): Actors matching the subject of a key that signed. Sorted.
        asserted (tuple[str, ...]): Actors no signing key vouches for. Not an accusation: the
            ordinary state of a merged contribution, and of every brain whose trust root names no
            subjects at all.
        legacy (tuple[str, ...]): Actors whose identifier predates the form rule, so they could
            never match a subject. Separated from ``asserted`` because the remedy differs -- one is
            a governance act, the other is a rewrite nobody can perform on published bytes.
        evidence_gaps (tuple[str, ...]): What could not be read, so the comparison went unmade.
    """

    snapshot: OciDigest
    verified: tuple[str, ...] = ()
    asserted: tuple[str, ...] = ()
    legacy: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether the comparison could be made at all, with no missing evidence."""
        return not self.evidence_gaps

    @property
    def is_fully_vouched(self) -> bool:
        """Whether every actor this snapshot introduces is backed by a signature.

        Never a precondition for anything. It is the strongest thing attribution can say, and a
        brain that cannot say it is not thereby suspect.
        """
        return not self.asserted and not self.legacy

    @property
    def detail(self) -> str:
        """Operator-facing explanation of what is unvouched."""
        parts = []
        if self.asserted:
            parts.append(f"no signing key vouches for {', '.join(self.asserted)}")
        if self.legacy:
            parts.append(f"identifiers that predate the actor-id rule: {', '.join(self.legacy)}")
        parts.extend(self.evidence_gaps)
        return "; ".join(parts)


def check_attribution(
    store: BlockStore,
    snapshot: Snapshot,
    subjects: Iterable[str],
    parent: Snapshot | None = None,
) -> AttributionReport:
    """
    Compare the actors a snapshot introduces against the subjects of the keys that signed it.

    Only the records *this* snapshot adds are examined. Judging inherited ones would re-report every
    ancestor's contributors at every position, and would mean a brain grew steadily more unvouched
    the longer it lived -- which says nothing about the snapshot in hand.

    Args:
        store (BlockStore): Where blocks and documents live.
        snapshot (Snapshot): The snapshot to examine.
        subjects (Iterable[str]): The subjects of the keys that signed it.
        parent (Snapshot | None): Its first parent, when already resolved.

    Returns:
        AttributionReport: What the signatures reach, what they do not, and what could not be asked.
    """
    vouched = set(subjects)
    reference = snapshot.modules.get(MemoryType.PROVENANCE)
    if reference is None:
        return AttributionReport(snapshot=snapshot.digest)

    composition = _composition(store, reference)
    if composition is None:
        return AttributionReport(
            snapshot=snapshot.digest,
            evidence_gaps=("the provenance composition is not resolvable",),
        )

    introduced = set(composition.block_ids) - _inherited(store, snapshot, parent)
    claimed: set[str] = set()
    gaps: list[str] = []
    for block_id in sorted(introduced, key=lambda value: value.raw):
        actor = _actor_of(store, block_id)
        if actor is None:
            gaps.append(f"provenance block {block_id.short} is not readable")
            continue
        claimed.add(actor.id)

    verified = sorted(actor for actor in claimed if actor in vouched)
    unvouched = sorted(actor for actor in claimed - set(verified))
    return AttributionReport(
        snapshot=snapshot.digest,
        verified=tuple(verified),
        asserted=tuple(actor for actor in unvouched if is_actor_id(actor)),
        legacy=tuple(actor for actor in unvouched if not is_actor_id(actor)),
        evidence_gaps=tuple(gaps),
    )


def _inherited(store: BlockStore, snapshot: Snapshot, parent: Snapshot | None) -> set[BlockId]:
    """The provenance blocks the first parent already held.

    An unresolvable parent yields the empty set, which makes every record look introduced. That
    over-reports rather than under-reports, and over-reporting is the safe direction for a check
    that never blocks: the alternative would let a truncated history hide who a snapshot brought in.
    """
    if snapshot.first_parent is None:
        return set()
    resolved = parent if parent is not None else _snapshot(store, snapshot.first_parent)
    if resolved is None:
        return set()
    reference = resolved.modules.get(MemoryType.PROVENANCE)
    if reference is None:
        return set()
    composition = _composition(store, reference)
    return set(composition.block_ids) if composition is not None else set()


def _actor_of(store: BlockStore, block_id: BlockId) -> Actor | None:
    if not store.is_resolvable(block_id):
        return None
    try:
        block = store.get_block(block_id)
    except BoltzmannError:
        return None
    if not isinstance(block, ProvenanceBlock | ProvenanceBlockV2):
        return None
    return block.record.actor


def _snapshot(store: BlockStore, digest: OciDigest) -> Snapshot | None:
    try:
        return Snapshot.from_document(store.get_bytes(digest))
    except (ValueError, BoltzmannError):
        return None


def _composition(store: BlockStore, reference: ModuleRef) -> Composition | None:
    try:
        composition = Composition.from_document(store.get_bytes(reference.composition))
    except (ValueError, BoltzmannError):
        return None
    if composition.memory_type is not reference.memory_type or composition.root != reference.root:
        return None
    return composition
