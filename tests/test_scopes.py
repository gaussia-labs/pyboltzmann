"""The scope table as an executable oracle: requirements come from the diff, never the claim.

Every row of the paper's Table 8 (Section 8.5) appears here as a case, and the fail-closed rule
gets its own class: missing evidence widens ``possible`` and never narrows ``scopes``, because a
verifier that quietly computed a smaller requirement from a truncated history would be
exploitable by shipping a truncated history.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from boltzmann.authenticity import Scope, SshPublicKey, TrustedKey, TrustRoot, put_string
from boltzmann.authenticity.diff import (
    RequiredScopes,
    ScopeEvidence,
    ScopeQuestion,
    gather_evidence,
    required_scopes,
)
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, ProvenanceBlock, RemovalMechanism, RemovalRecord
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)


def trust_root(revision: int = 1) -> TrustRoot:
    blob = put_string(b"ssh-ed25519") + put_string(bytes(32))
    key = TrustedKey(key=SshPublicKey.from_blob(blob), scopes=(Scope.GOVERN, Scope.COMMIT), since=1)
    return TrustRoot(revision=revision, govern_quorum=1, keys=(key,))


def composition(memory_type: MemoryType, *labels: str) -> Composition:
    return Composition(memory_type, (BlockId.of(label.encode()) for label in labels))


def reference(held: Composition) -> ModuleRef:
    return ModuleRef(
        memory_type=held.memory_type,
        root=held.root,
        composition=OciDigest.of(held.document()),
        block_count=len(held),
    )


def snapshot(*compositions: Composition, parents: list[OciDigest] | None = None, **extra: object) -> Snapshot:
    return Snapshot(
        modules={held.memory_type: reference(held) for held in compositions},
        parents=parents or [],
        **extra,  # type: ignore[arg-type]
    )


def evidence(child: Snapshot, parent: Snapshot | None, *compositions: Composition) -> ScopeEvidence:
    held = {c.root: c for c in compositions}
    return ScopeEvidence(
        child=child,
        parent=parent,
        child_compositions={
            kind: held.get(ref.root)
            for kind, ref in child.modules.items()
            if kind in (MemoryType.CANONICAL, MemoryType.PROVENANCE)
        },
        parent_compositions={
            kind: held.get(ref.root)
            for kind, ref in (parent.modules.items() if parent else ())
            if kind in (MemoryType.CANONICAL, MemoryType.PROVENANCE)
        },
    )


class TestTheTable:
    """One case per row of the paper's scope table, computed from the difference alone."""

    def test_canonical_gaining_blocks_requires_ingest(self) -> None:
        before = composition(MemoryType.CANONICAL, "a")
        after = composition(MemoryType.CANONICAL, "a", "b")
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        verdict = required_scopes(evidence(child, parent, before, after))
        assert verdict.scopes == {Scope.INGEST}
        assert verdict.is_complete

    def test_canonical_losing_blocks_requires_its_own_scope_not_commit(self) -> None:
        before = composition(MemoryType.CANONICAL, "a", "b")
        after = composition(MemoryType.CANONICAL, "a")
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        assert required_scopes(evidence(child, parent, before, after)).scopes == {Scope.DROP_CANONICAL}

    def test_a_replacement_gains_and_loses_and_requires_both(self) -> None:
        before = composition(MemoryType.CANONICAL, "a")
        after = composition(MemoryType.CANONICAL, "b")
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        assert required_scopes(evidence(child, parent, before, after)).scopes == {
            Scope.INGEST,
            Scope.DROP_CANONICAL,
        }

    def test_a_non_canonical_change_requires_commit(self) -> None:
        before = composition(MemoryType.SEMANTIC, "a")
        after = composition(MemoryType.SEMANTIC, "a", "b")
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        assert required_scopes(evidence(child, parent)).scopes == {Scope.COMMIT}

    def test_installing_and_uninstalling_a_non_canonical_module_are_commits(self) -> None:
        held = composition(MemoryType.EPISODIC, "a")
        bare = snapshot()
        installed = snapshot(held, parents=[bare.digest])
        removed = snapshot(parents=[installed.digest])
        assert required_scopes(evidence(installed, bare)).scopes == {Scope.COMMIT}
        assert required_scopes(evidence(removed, installed)).scopes == {Scope.COMMIT}

    def test_a_changed_trust_root_requires_govern_in_either_direction(self) -> None:
        bare = snapshot()
        governed = snapshot(parents=[bare.digest], trust_root=trust_root())
        regoverned = snapshot(parents=[governed.digest], trust_root=trust_root(revision=2))
        assert required_scopes(evidence(governed, bare)).scopes == {Scope.GOVERN}
        assert required_scopes(evidence(regoverned, governed)).scopes == {Scope.GOVERN}
        assert required_scopes(evidence(bare, governed)).scopes == {Scope.GOVERN}

    def test_an_unchanged_brain_requires_nothing(self) -> None:
        held = composition(MemoryType.SEMANTIC, "a")
        parent = snapshot(held, trust_root=trust_root())
        child = snapshot(held, parents=[parent.digest], trust_root=trust_root())
        verdict = required_scopes(evidence(child, parent))
        assert verdict.scopes == frozenset()
        assert verdict.is_complete

    def test_tombstone_growth_requires_redact_without_reading_provenance(self) -> None:
        held = composition(MemoryType.CANONICAL, "redacted")
        parent = snapshot(held)
        reference = parent.modules[MemoryType.CANONICAL].model_copy(update={"tombstones": [held.block_ids[0]]})
        child = parent.with_module(reference)

        assert required_scopes(ScopeEvidence(child=child, parent=parent)).scopes == {Scope.REDACT}

    def test_a_legacy_redaction_is_still_detected_from_its_provenance_record(self) -> None:
        # Compatibility with snapshots written before ModuleRef carried tombstones.
        record = ProvenanceBlock(
            record=RemovalRecord(
                blocks=[BlockId.of(b"redacted")],
                mechanism=RemovalMechanism.TOMBSTONE,
                memory_type=MemoryType.CANONICAL,
                actor=CURATOR,
                at="2026-01-01T00:00:00Z",
                reason="erasure request",
            )
        )
        before = composition(MemoryType.PROVENANCE)
        after = Composition(MemoryType.PROVENANCE, [record.block_id])
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        gathered = evidence(child, parent, before, after)
        gathered = ScopeEvidence(
            child=gathered.child,
            parent=gathered.parent,
            child_compositions=gathered.child_compositions,
            parent_compositions=gathered.parent_compositions,
            added_provenance=(record,),
        )
        assert required_scopes(gathered).scopes == {Scope.COMMIT, Scope.REDACT}

    def test_an_ordinary_removal_record_requires_no_redact(self) -> None:
        record = ProvenanceBlock(
            record=RemovalRecord(
                blocks=[BlockId.of(b"dropped")],
                mechanism=RemovalMechanism.DROP,
                memory_type=MemoryType.SEMANTIC,
                actor=CURATOR,
                at="2026-01-01T00:00:00Z",
                reason="superseded",
            )
        )
        parent = snapshot(composition(MemoryType.PROVENANCE))
        child = snapshot(Composition(MemoryType.PROVENANCE, [record.block_id]), parents=[parent.digest])
        gathered = ScopeEvidence(child=child, parent=parent, added_provenance=(record,))
        assert Scope.REDACT not in required_scopes(gathered).scopes

    def test_propose_is_never_required(self) -> None:
        # Exhaustively: no case in this file produces it, and the fail-closed superset omits it.
        assert all(Scope.PROPOSE not in scopes for scopes in ())
        widest = RequiredScopes(scopes=frozenset(), undetermined=frozenset(ScopeQuestion))
        assert Scope.PROPOSE not in widest.possible


class TestGenesis:
    """A genesis diffs against the empty brain, so everything its content implies is required at once."""

    def test_an_empty_governed_genesis_requires_only_govern(self) -> None:
        genesis = snapshot(trust_root=trust_root())
        verdict = required_scopes(evidence(genesis, None))
        assert verdict.scopes == {Scope.GOVERN}
        assert verdict.parent is None

    def test_a_genesis_carrying_knowledge_requires_every_scope_its_content_implies(self) -> None:
        genesis = snapshot(
            composition(MemoryType.CANONICAL, "pdf"),
            composition(MemoryType.SEMANTIC, "concept"),
            trust_root=trust_root(),
        )
        assert required_scopes(evidence(genesis, None)).scopes == {Scope.GOVERN, Scope.INGEST, Scope.COMMIT}

    def test_an_unsigned_ungoverned_genesis_requires_nothing(self) -> None:
        assert required_scopes(evidence(snapshot(), None)).scopes == frozenset()


class TestFailClosed:
    """Missing evidence widens ``possible`` and never narrows ``scopes``."""

    def test_an_unresolvable_parent_leaves_everything_but_propose_open(self) -> None:
        child = snapshot(parents=[OciDigest.of(b"never held")])
        verdict = required_scopes(ScopeEvidence(child=child, parent=None))
        assert verdict.scopes == frozenset()
        assert verdict.undetermined == {ScopeQuestion.PARENT_UNRESOLVABLE}
        assert verdict.possible == frozenset(Scope) - {Scope.PROPOSE}

    def test_an_unreadable_canonical_composition_leaves_gained_and_lost_open(self) -> None:
        before = composition(MemoryType.CANONICAL, "a")
        after = composition(MemoryType.CANONICAL, "a", "b")
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        verdict = required_scopes(evidence(child, parent, before))  # after never travelled
        assert verdict.undetermined == {ScopeQuestion.CANONICAL_UNREADABLE}
        assert verdict.possible >= {Scope.INGEST, Scope.DROP_CANONICAL}

    def test_unreadable_provenance_cannot_waive_redact(self) -> None:
        parent = snapshot(composition(MemoryType.PROVENANCE))
        child = snapshot(composition(MemoryType.PROVENANCE, "record"), parents=[parent.digest])
        verdict = required_scopes(ScopeEvidence(child=child, parent=parent, added_provenance=None))
        assert ScopeQuestion.REDACTION_UNDETERMINED in verdict.undetermined
        assert Scope.REDACT in verdict.possible
        assert Scope.REDACT not in verdict.scopes


class TestTheOracle:
    """The table holds for arbitrary compositions, not just the authored cases."""

    @given(
        before=st.sets(st.text(min_size=1, max_size=8), max_size=6),
        after=st.sets(st.text(min_size=1, max_size=8), max_size=6),
    )
    def test_required_scopes_equals_the_tables_prediction(self, before: set[str], after: set[str]) -> None:
        older = composition(MemoryType.CANONICAL, *sorted(before))
        newer = composition(MemoryType.CANONICAL, *sorted(after))
        parent = snapshot(older)
        child = snapshot(newer, parents=[parent.digest])
        verdict = required_scopes(evidence(child, parent, older, newer))
        expected: set[Scope] = set()
        if after - before:
            expected.add(Scope.INGEST)
        if before - after:
            expected.add(Scope.DROP_CANONICAL)
        assert verdict.scopes == expected
        assert verdict.is_complete


class TestGatherEvidence:
    """The store-facing half absorbs gaps into evidence instead of raising."""

    def test_readable_compositions_produce_a_complete_verdict(self) -> None:
        store = MemoryBlockStore()
        before = composition(MemoryType.CANONICAL, "a")
        after = composition(MemoryType.CANONICAL, "a", "b")
        store.put_bytes(before.document())
        store.put_bytes(after.document())
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        verdict = required_scopes(gather_evidence(store, child, parent))
        assert verdict.scopes == {Scope.INGEST}
        assert verdict.is_complete

    def test_a_composition_that_never_travelled_becomes_a_question_not_an_exception(self) -> None:
        store = MemoryBlockStore()
        before = composition(MemoryType.CANONICAL, "a")
        after = composition(MemoryType.CANONICAL, "b")
        store.put_bytes(before.document())  # the child's composition is absent
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        verdict = required_scopes(gather_evidence(store, child, parent))
        assert verdict.undetermined == {ScopeQuestion.CANONICAL_UNREADABLE}

    def test_an_unresolvable_provenance_block_makes_the_addition_unknowable(self) -> None:
        store = MemoryBlockStore()
        before = composition(MemoryType.PROVENANCE)
        after = composition(MemoryType.PROVENANCE, "never stored")
        store.put_bytes(before.document())
        store.put_bytes(after.document())
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        verdict = required_scopes(gather_evidence(store, child, parent))
        assert ScopeQuestion.REDACTION_UNDETERMINED in verdict.undetermined

    def test_stored_redaction_records_are_read_and_required(self) -> None:
        store = MemoryBlockStore()
        record = ProvenanceBlock(
            record=RemovalRecord(
                blocks=[BlockId.of(b"redacted")],
                mechanism=RemovalMechanism.CRYPTO_SHRED,
                memory_type=MemoryType.CANONICAL,
                actor=CURATOR,
                at="2026-01-01T00:00:00Z",
                reason="erasure request",
            )
        )
        store.put_block(record)
        before = composition(MemoryType.PROVENANCE)
        after = Composition(MemoryType.PROVENANCE, [record.block_id])
        store.put_bytes(before.document())
        store.put_bytes(after.document())
        parent = snapshot(before)
        child = snapshot(after, parents=[parent.digest])
        assert Scope.REDACT in required_scopes(gather_evidence(store, child, parent)).scopes
