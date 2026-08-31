"""The removal ledger is a verifier-side invariant, not an honor-system write rule."""

import pytest

from boltzmann.authenticity.authenticator import Authenticator, FindingKind
from boltzmann.authenticity.removals import check_removal_invariant
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    ProvenanceBlock,
    RemovalMechanism,
    RemovalRecord,
)
from boltzmann.exceptions import RemovalInvariantError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.module import Module
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator@example.org", kind=ActorKind.HUMAN)


def persisted(store: MemoryBlockStore, composition: Composition):
    return Module(composition.memory_type, store, composition).persist()


def removed_snapshot(*, recorded: bool) -> tuple[MemoryBlockStore, Snapshot]:
    store = MemoryBlockStore()
    victim = BlockId.of(b"removed knowledge")
    before_composition = Composition(MemoryType.SEMANTIC, [victim])
    before = Snapshot(modules={MemoryType.SEMANTIC: persisted(store, before_composition)})
    store.put_bytes(before.canonical_bytes())

    modules = {MemoryType.SEMANTIC: persisted(store, Composition(MemoryType.SEMANTIC))}
    if recorded:
        removal = ProvenanceBlock(
            record=RemovalRecord(
                blocks=[victim],
                mechanism=RemovalMechanism.DROP,
                memory_type=MemoryType.SEMANTIC,
                actor=CURATOR,
                at="2026-08-28T00:00:00Z",
                reason="obsolete",
            )
        )
        store.put_block(removal)
        provenance = Composition(MemoryType.PROVENANCE, [removal.block_id])
        modules[MemoryType.PROVENANCE] = persisted(store, provenance)

    child = Snapshot(modules=modules, parents=[before.digest])
    store.put_bytes(child.canonical_bytes())
    return store, child


def test_an_unrecorded_absence_fails_whole_snapshot_verification() -> None:
    store, snapshot = removed_snapshot(recorded=False)
    assert not check_removal_invariant(store, snapshot).is_valid


def test_a_reachable_removal_record_satisfies_the_invariant() -> None:
    store, snapshot = removed_snapshot(recorded=True)
    assert check_removal_invariant(store, snapshot).is_valid


def test_the_invariant_does_not_depend_on_any_field_being_present() -> None:
    """It is a statement about compositions, so nothing in a snapshot can opt out of it.

    An earlier version keyed the check on whether a module reference carried a tombstones member,
    which made the field a protocol-version marker as well as a fact -- and an attacker could then
    turn the check off by omitting it.
    """
    store, snapshot = removed_snapshot(recorded=False)
    downgraded = snapshot.model_copy(
        update={
            "modules": {
                memory_type: reference.model_copy(update={"tombstones": None})
                for memory_type, reference in snapshot.modules.items()
            }
        }
    )

    assert not check_removal_invariant(store, downgraded).is_valid


def test_an_unresolvable_parent_is_undecidable_rather_than_failed() -> None:
    """Refusing here would refuse every brain that pruned its history, which the protocol permits.

    Passing silently would be worse: a truncated history would turn the check off. So the question
    is reported as one that could not be put.
    """
    store, snapshot = removed_snapshot(recorded=True)
    orphan = Snapshot(modules=snapshot.modules, parents=[OciDigest.of(b"a parent nobody holds")])

    integrity = check_removal_invariant(store, orphan)

    assert integrity.is_valid, "nothing is unaccounted for; the difference simply cannot be taken"
    assert not integrity.is_complete
    assert "not resolvable" in integrity.detail


def test_an_undecidable_ledger_is_reported_without_blocking() -> None:
    store, snapshot = removed_snapshot(recorded=True)
    orphan = Snapshot(modules=snapshot.modules, parents=[OciDigest.of(b"a parent nobody holds")])
    store.put_bytes(orphan.canonical_bytes())

    report = Authenticator(store).authenticate(orphan)

    assert report.has(FindingKind.REMOVAL_UNDECIDABLE)
    assert not report.has(FindingKind.REMOVAL_INVARIANT)
    assert not any(f.blocking for f in report.findings if f.kind is FindingKind.REMOVAL_UNDECIDABLE)


def test_authentication_reports_and_rejects_an_unrecorded_absence() -> None:
    store, snapshot = removed_snapshot(recorded=False)
    report = Authenticator(store).authenticate(snapshot)

    assert report.has(FindingKind.REMOVAL_INVARIANT)
    with pytest.raises(RemovalInvariantError):
        report.require_authorized()
