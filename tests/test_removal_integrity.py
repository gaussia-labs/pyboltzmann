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
from boltzmann.identity.digest import BlockId
from boltzmann.module.composition import Composition
from boltzmann.module.module import Module
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)


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


def test_a_modern_child_cannot_omit_the_field_to_disable_the_invariant() -> None:
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


def test_authentication_reports_and_rejects_an_unrecorded_absence() -> None:
    store, snapshot = removed_snapshot(recorded=False)
    report = Authenticator(store).authenticate(snapshot)

    assert report.has(FindingKind.REMOVAL_INVARIANT)
    with pytest.raises(RemovalInvariantError):
        report.require_authorized()
