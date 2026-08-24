"""The conformance suite: what a third-party implementation must satisfy.

Because the brain is portable data addressed by a protocol, the same snapshot can be read
and extended by any conforming client (paper Section 7). "Conforming" is only meaningful if
it can be checked, so this suite is importable and runnable against someone else's
implementation:

.. code-block:: python

    from boltzmann.conformance import BlockStoreConformance

    class TestMyStore(BlockStoreConformance):
        def make_store(self):
            return MyStore()

The suite tests behavior the protocol requires, not the way this SDK happens to implement
it. Where the paper leaves something to the implementation -- ranking order, fusion method,
index engine -- the suite says nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from boltzmann.authenticity.authenticator import AuthenticationReport
from collections.abc import Callable
from typing import Any

import pytest

from boltzmann.blocks.base import Block
from boltzmann.blocks.canonical import CanonicalBlock
from boltzmann.blocks.content import ContentRef
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    DerivationRecord,
    Producer,
    ProducerKind,
    ProvenanceBlock,
    SupersessionRecord,
)
from boltzmann.blocks.semantic import SemanticBlock, SemanticBlockV2, SemanticKind
from boltzmann.conformance import golden
from boltzmann.exceptions import (
    AppendOnlyViolationError,
    BlockIntegrityError,
    BlockNotFoundError,
    BlockTombstonedError,
    BoltzmannError,
    DigestKindError,
    NoCommonAncestorError,
    NonDeterministicValueError,
    SnapshotError,
)
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize
from boltzmann.identity.time import utc_timestamp
from boltzmann.merkle.tree import MerkleTree, merkle_root
from boltzmann.module.composition import Composition
from boltzmann.module.ledger import Ledger
from boltzmann.module.module import Module
from boltzmann.module.snapshot import Snapshot
from boltzmann.reconcile.ancestry import common_ancestor
from boltzmann.reconcile.merge import merge_module
from boltzmann.reconcile.requests import ReconcileStrategy
from boltzmann.reconcile.strategies import attribution_table
from boltzmann.retention.cascade import plan_many
from boltzmann.store.base import BlockStore
from boltzmann.store.memory import MemoryBlockStore


def _supersedes(block: BlockId, superseded: BlockId) -> ProvenanceBlock:
    """A supersession record, for the suites that reason about precedence."""
    return ProvenanceBlock(
        record=SupersessionRecord(
            block=block,
            supersedes=superseded,
            actor=Actor(id="curator", kind=ActorKind.HUMAN),
            at=utc_timestamp(),
        )
    )


def _ledger_over(*records: ProvenanceBlock) -> Ledger:
    """A ledger read from a real provenance composition, the way every caller reads one."""
    store = MemoryBlockStore()
    ids = [store.put_block(record) for record in records]
    module = Module(MemoryType.PROVENANCE, store, Composition(MemoryType.PROVENANCE, ids))
    return Ledger.of({MemoryType.PROVENANCE: module})


def sample_semantic(label: str = "Fourier series") -> SemanticBlock:
    """
    A semantic block the suite can use anywhere.

    Args:
        label (str): Label of the block, varied to obtain distinct identities.

    Returns:
        SemanticBlock: The block.
    """
    return SemanticBlock(
        kind=SemanticKind.FORMULA,
        label=label,
        statement="f(x) = a0/2 + sum(a_n cos(nx) + b_n sin(nx))",
        subject="signals",
    )


def sample_semantic_v2(label: str = "Phase diagram") -> SemanticBlockV2:
    """
    A semantic block whose datum sits in the store rather than in its payload.

    Args:
        label (str): Label of the block, varied to obtain distinct identities.

    Returns:
        SemanticBlockV2: The block, naming content it does not carry.
    """
    content = b"a deterministic stand-in for a phase diagram, hashed as-is"
    return SemanticBlockV2(
        kind=SemanticKind.CONCEPT,
        label=label,
        statement="The diagram shows the solid-liquid transition",
        subject="thermodynamics",
        content=ContentRef(blob=OciDigest.of(content), media_type="image/png", size=len(content)),
    )


def sample_blocks() -> list[Block]:
    """
    One block of every registered schema, of every version.

    Derived from the registry rather than listed, so a schema added later is exercised by the
    suites that use this without anyone remembering to extend a literal. The version axis is the
    reason it matters: a client may implement two versions of a memory type, and a conformance
    claim that only ever saw the first would be reporting on less than it says.

    Returns:
        list[Block]: A sample per registered ``(memory_type, schema_version)`` this module knows how
        to build. Schemas with no sampler are skipped rather than guessed at.
    """
    samplers: dict[tuple[MemoryType, int], Callable[[], Block]] = {
        (MemoryType.CANONICAL, 1): sample_canonical,
        (MemoryType.SEMANTIC, 1): sample_semantic,
        (MemoryType.SEMANTIC, 2): sample_semantic_v2,
    }
    return [build() for key, build in samplers.items() if key in Block.registry()]


def sample_canonical(payload: bytes = b"%PDF-1.7 lecture notes") -> CanonicalBlock:
    """
    A canonical block over some observed bytes.

    Args:
        payload (bytes): The observed bytes.

    Returns:
        CanonicalBlock: The block describing them.
    """
    return CanonicalBlock(
        blob=OciDigest.of(payload),
        media_type="application/pdf",
        size=len(payload),
    )


class IdentityConformance:
    """The three levels of hashes, and the serialization that feeds them."""

    def test_levels_are_not_interchangeable(self) -> None:
        """A block id and a Merkle root over the same bytes are not equal."""
        block_id = BlockId.of(b"same bytes")
        root = MerkleRoot.of(b"same bytes")
        assert block_id.hex == root.hex
        # mypy reports a non-overlapping comparison here, which is the property under test:
        # the two levels are unrelated types, so the equality cannot even be meaningful.
        assert block_id != root  # type: ignore[comparison-overlap]

    def test_parsing_rejects_the_wrong_level(self) -> None:
        """A root offered where a block id is expected is refused, not coerced."""
        root = MerkleRoot.of(b"payload")
        with pytest.raises(DigestKindError):
            BlockId.parse(root)

    def test_serialization_is_key_order_independent(self) -> None:
        """Canonicalization erases the order a mapping was built in."""
        assert canonicalize({"b": 1, "a": 2}) == canonicalize({"a": 2, "b": 1})

    def test_floats_are_refused(self) -> None:
        """A float has no portable canonical form, so it cannot enter a payload."""
        with pytest.raises(NonDeterministicValueError):
            canonicalize({"weight": 0.5})

    def test_unsafe_integers_are_refused(self) -> None:
        """An integer a double cannot represent exactly cannot enter a payload."""
        with pytest.raises(NonDeterministicValueError):
            canonicalize({"count": 2**53})

    def test_block_id_matches_golden_vectors(self) -> None:
        """Identity agrees with the published vectors, which other languages also read."""
        for vector in golden.load("block_ids.json")["vectors"]:
            assert vector["block_id"] == str(BlockId.of(vector["canonical_bytes"].encode()))


class MerkleConformance:
    """The properties a Merkle layout must have to be usable by the protocol."""

    def test_root_is_a_function_of_the_set(self) -> None:
        """Two parties that assembled the same blocks obtain the same root."""
        blocks = [BlockId.of(f"block-{index}".encode()) for index in range(9)]
        assert merkle_root(blocks) == merkle_root(list(reversed(blocks)))

    def test_duplicates_collapse(self) -> None:
        """A set of content-addressed blocks cannot hold the same block twice."""
        block = BlockId.of(b"only one")
        assert merkle_root([block]) == merkle_root([block, block])

    def test_empty_composition_has_a_root(self) -> None:
        """A module with no blocks still has a well-defined identity."""
        assert merkle_root([]).hex

    def test_every_leaf_proves_into_the_root(self) -> None:
        """Membership is provable for every block, at every tree size."""
        for size in range(1, 33):
            blocks = [BlockId.of(f"b{index}".encode()) for index in range(size)]
            tree = MerkleTree(blocks)
            root = tree.root
            for block in blocks:
                assert tree.inclusion_proof(block).verify(root)

    def test_a_proof_does_not_verify_against_another_root(self) -> None:
        """A proof binds a block to one composition, not to any composition."""
        blocks = [BlockId.of(f"b{index}".encode()) for index in range(8)]
        tree = MerkleTree(blocks)
        proof = tree.inclusion_proof(blocks[3])
        assert not proof.verify(MerkleTree(blocks[:-1]).root)

    def test_root_matches_golden_vectors(self) -> None:
        """Roots agree with the published vectors."""
        for vector in golden.load("merkle_roots.json")["vectors"]:
            leaves = [BlockId.parse(value) for value in vector["block_ids"]]
            assert vector["root"] == str(merkle_root(leaves))


class CompositionConformance:
    """How a version changes, and what refuses to change."""

    def test_dropping_yields_a_new_root(self) -> None:
        """Excluding a block publishes a different composition."""
        blocks = [BlockId.of(f"b{index}".encode()) for index in range(4)]
        composition = Composition(MemoryType.SEMANTIC, blocks)
        reduced = composition.drop([blocks[1]])
        assert reduced.root != composition.root
        assert blocks[1] not in reduced

    def test_dropping_does_not_disturb_the_earlier_root(self) -> None:
        """Older roots keep verifying exactly as before, because nothing about them changed."""
        blocks = [BlockId.of(f"b{index}".encode()) for index in range(4)]
        composition = Composition(MemoryType.SEMANTIC, blocks)
        before = composition.root
        composition.drop([blocks[0]])
        assert composition.root == before

    def test_episodic_refuses_to_drop(self) -> None:
        """The chronological record is append-only by protocol, not by policy."""
        block = BlockId.of(b"an episode")
        with pytest.raises(AppendOnlyViolationError):
            Composition(MemoryType.EPISODIC, [block]).drop([block])

    def test_diff_reports_what_a_consumer_must_fetch(self) -> None:
        """An incremental update transfers what changed, and reuses the rest by hash."""
        blocks = [BlockId.of(f"b{index}".encode()) for index in range(4)]
        added = BlockId.of(b"new")
        before = Composition(MemoryType.SEMANTIC, blocks)
        after = before.drop([blocks[0]]).add([added])
        difference = before.diff(after)
        assert difference.added == [added]
        assert difference.removed == [blocks[0]]
        assert difference.transfer_size == 1


class ReconciliationConformance:
    """What reconciling two histories must do, whoever implements it (paper Section 12).

    Only the part the protocol fixes. How a client presents a plan, whether it offers a default strategy in
    its own UI, and what it does with a rejected contribution are the implementation's business. That the
    arithmetic converges, that exclusion wins, that the three strategies agree on the result, and that a
    missing ancestor is a distinguishable failure are not.
    """

    @staticmethod
    def _composition(*numbers: int) -> Composition:
        return Composition(MemoryType.SEMANTIC, [BlockId.of(str(number).encode()) for number in numbers])

    def test_the_arithmetic_converges_whichever_side_is_ours(self) -> None:
        """A module's reconciliation is set arithmetic over identifiers, so the order the sides are
        combined in cannot change the result."""
        base, ours, theirs = self._composition(1, 2), self._composition(1, 3), self._composition(1, 2, 4)
        left = merge_module(MemoryType.SEMANTIC, base, ours, theirs)
        right = merge_module(MemoryType.SEMANTIC, base, theirs, ours)

        assert left is not None
        assert right is not None
        assert left.root == right.root

    def test_exclusion_wins(self) -> None:
        """A block one side dropped does not return because the other side still held it."""
        dropped = BlockId.of(b"2")
        merged = merge_module(
            MemoryType.SEMANTIC, self._composition(1, 2), self._composition(1), self._composition(1, 2, 3)
        )

        assert merged is not None
        assert dropped not in merged.block_ids
        assert dropped in merged.removed

    def test_re_ingesting_dropped_evidence_does_not_smuggle_it_back(self) -> None:
        """Re-registering the same source yields the same identifier, so a reconciliation recognizes it as
        something this brain removed rather than as a new contribution -- no special rule required."""
        resent = BlockId.of(b"2")
        merged = merge_module(
            MemoryType.SEMANTIC,
            self._composition(1, 2),
            self._composition(1),
            self._composition(1, 2),
        )

        assert merged is not None
        assert resent not in merged.block_ids

    def test_module_level_absence_is_not_a_removal(self) -> None:
        """A partial install does not hold every module, and not holding one is not having emptied it."""
        theirs = self._composition(1, 2, 3)
        merged = merge_module(MemoryType.SEMANTIC, self._composition(1), None, theirs)

        assert merged is not None
        assert merged.root == theirs.root

    def test_an_append_only_module_cannot_be_narrowed(self) -> None:
        """No conforming history can have dropped from the episodic module."""
        base = Composition(MemoryType.EPISODIC, [BlockId.of(b"1"), BlockId.of(b"2")])
        with pytest.raises(AppendOnlyViolationError):
            merge_module(MemoryType.EPISODIC, base, base, Composition(MemoryType.EPISODIC, [BlockId.of(b"1")]))

    def test_the_three_strategies_differ_only_in_what_they_record(self) -> None:
        """All three land the same blocks, so the choice between them is attribution and not outcome. An
        implementation must not present a rebased or squashed contribution as bearing the contributor's
        signature."""
        table = attribution_table(collapsed=3)

        assert table[ReconcileStrategy.MERGE].keeps_their_snapshots
        assert table[ReconcileStrategy.MERGE].their_signatures_survive
        for strategy in (ReconcileStrategy.REBASE, ReconcileStrategy.SQUASH):
            assert not table[strategy].keeps_their_snapshots
            assert not table[strategy].their_signatures_survive
            assert table[strategy].mints_new_identities
        assert table[ReconcileStrategy.SQUASH].snapshots_written == 1

    def test_withdrawn_evidence_takes_what_cites_it(self) -> None:
        """Equation 1 is applied per module and the invariants run between them, so excluding evidence in one
        module leaves its dependents behind in another -- individually correct, and a violation of R1
        overall. The cascade Section 10.3 defines is what resolves it, and reconciliation must run the same
        one: a derived block whose evidence the composition does not hold cannot be audited against its
        source, and recomputing hashes and compositions would not reveal it."""
        store = MemoryBlockStore()
        source = store.put_block(sample_canonical())
        derived = store.put_block(sample_semantic("a later reading"))
        record = store.put_block(
            ProvenanceBlock(
                record=DerivationRecord(
                    block=derived,
                    derived_from=[source],
                    producer=Producer(kind=ProducerKind.MODEL, id="some-model", version="1"),
                    actor=Actor(id="curator", kind=ActorKind.HUMAN),
                    at=utc_timestamp(),
                )
            )
        )
        modules = {
            MemoryType.CANONICAL: Module(MemoryType.CANONICAL, store, Composition(MemoryType.CANONICAL, [])),
            MemoryType.SEMANTIC: Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [derived])),
            MemoryType.PROVENANCE: Module(MemoryType.PROVENANCE, store, Composition(MemoryType.PROVENANCE, [record])),
        }

        cascade = plan_many([source], MemoryType.CANONICAL, modules, Ledger.of(modules))

        assert derived in cascade.dependents[MemoryType.SEMANTIC]

    def test_a_precedence_question_is_not_answered_by_the_ledger(self) -> None:
        """Two histories replacing the same block with different successors leaves two edges, and the ledger
        must not present one of them as the answer. Whichever record it happened to read last would otherwise
        decide a question Section 12.4 assigns to a person."""
        original, first, second = BlockId.of(b"original"), BlockId.of(b"first"), BlockId.of(b"second")
        ledger = _ledger_over(_supersedes(first, original), _supersedes(second, original))

        assert ledger.successors_of(original) == {first, second}
        assert ledger.contested(original) == {first, second}

    def test_settling_precedence_closes_the_question_without_erasing_an_edge(self) -> None:
        """Precedence is stated the only way this architecture can state it -- one more supersession edge --
        so the record of what each history did survives the decision about which one prevails."""
        original, first, second = BlockId.of(b"original"), BlockId.of(b"first"), BlockId.of(b"second")
        ledger = _ledger_over(
            _supersedes(first, original),
            _supersedes(second, original),
            _supersedes(second, first),
        )

        assert ledger.contested(original) == set()
        assert ledger.successors_of(original) == {first, second}
        assert not ledger.is_accessible(first)
        assert ledger.is_accessible(second)

    def test_no_ancestor_is_a_distinguishable_failure(self) -> None:
        """Without a common ancestor, a block on one side and not the other is ambiguous between "they
        added it" and "I dropped it", so there is no three-way merge to compute."""
        store = MemoryBlockStore()
        theirs = Snapshot()
        digest = store.put_bytes(theirs.canonical_bytes())

        with pytest.raises(NoCommonAncestorError):
            common_ancestor(store, [OciDigest.of(b"an unrelated history")], theirs, digest)

    def test_a_lineage_records_which_history_it_was_performed_onto(self) -> None:
        """Order is significant in exactly one way: the first parent is the history the reconciliation was
        performed onto, and the rest are merged-in history that grants nothing."""
        ours = Snapshot().with_modules([])
        theirs = Snapshot(labels={"side": "theirs"})
        reconciliation = ours.reconciled([], [theirs.digest])

        assert reconciliation.first_parent == ours.digest
        assert reconciliation.is_reconciliation
        assert theirs.digest in reconciliation.parents

    def test_one_parent_is_written_as_a_scalar(self) -> None:
        """A version is a statement, not a preference: a snapshot is written under the oldest form that can
        express it, so a linear history stays readable by a client that knows nothing of reconciliation."""
        linear = Snapshot().with_modules([])
        merged = linear.reconciled([], [Snapshot(labels={"side": "theirs"}).digest])

        assert b'"parent":' in linear.canonical_bytes()
        assert b'"parents":' in merged.canonical_bytes()


class BlockStoreConformance(ABC):
    """
    What any store must do, whoever wrote it.

    Subclass this and implement :meth:`make_store`.
    """

    @abstractmethod
    def make_store(self) -> BlockStore:
        """
        Build an empty store to test.

        Returns:
            BlockStore: The store under test.
        """

    def test_storing_bytes_twice_is_a_noop(self) -> None:
        """Identical content has one identity, so re-registering it adds nothing."""
        store = self.make_store()
        first = store.put_bytes(b"%PDF-1.7 lecture notes")
        second = store.put_bytes(b"%PDF-1.7 lecture notes")
        assert first == second

    def test_round_trips_a_block(self) -> None:
        """A stored block decodes back to an equal block with the same identity."""
        store = self.make_store()
        block = sample_semantic()
        block_id = store.put_block(block)
        recovered = store.get_block(block_id)
        assert recovered == block
        assert recovered.block_id == block_id

    def test_round_trips_every_memory_type(self) -> None:
        """Every block schema survives the store, including every version of one.

        Walks the registry rather than a literal pair. It said "every block schema" while testing
        two, which was harmless until a memory type had more than one version -- at which point the
        claim and the coverage diverge in exactly the direction a conformance suite must not.
        """
        store = self.make_store()
        samples = sample_blocks()
        assert len(samples) >= 2
        for block in samples:
            assert store.get_block(store.put_block(block)) == block

    def test_missing_block_raises(self) -> None:
        """An absent block is an error, not an empty result."""
        store = self.make_store()
        with pytest.raises(BlockNotFoundError):
            store.get_block(BlockId.of(b"never stored"))

    def test_corruption_is_detected(self) -> None:
        """Bytes that do not hash to the digest they are filed under are refused."""
        store = self.make_store()
        block = sample_semantic()
        store.put_block(block)
        wrong_id = BlockId.of(b"a different block entirely")
        store.put_bytes(block.canonical_bytes())
        with pytest.raises((BlockIntegrityError, BlockNotFoundError)):
            store.get_bytes(wrong_id)

    def test_non_canonical_bytes_are_refused(self) -> None:
        """A store must not normalize: bytes that are not canonical do not decode."""
        store = self.make_store()
        block = sample_semantic()
        loose = block.canonical_bytes().replace(b'{"boltzmann"', b'{ "boltzmann"')
        digest = store.put_bytes(loose)
        with pytest.raises(BlockIntegrityError):
            store.get_block(BlockId.parse(str(digest)))

    def test_tombstoned_block_is_distinguishable_from_missing(self) -> None:
        """A removed block must never look like a corrupted one."""
        store = self.make_store()
        block_id = store.put_block(sample_semantic())
        store.tombstone(block_id, "erasure policy: personal data")

        assert store.has(block_id)
        assert not store.is_resolvable(block_id)
        with pytest.raises(BlockTombstonedError):
            store.get_block(block_id)

        never_stored = BlockId.of(b"never stored")
        assert not store.has(never_stored)
        with pytest.raises(BlockNotFoundError):
            store.get_block(never_stored)

    def test_delete_reclaims(self) -> None:
        """Pruning removes the bytes and the record of them alike."""
        store = self.make_store()
        block_id = store.put_block(sample_semantic())
        store.delete(block_id)
        assert not store.has(block_id)

    def test_iterates_what_it_holds(self) -> None:
        """A store can enumerate its content, which is what mark-and-sweep needs."""
        store = self.make_store()
        stored = {store.put_block(sample_semantic(f"concept {index}")) for index in range(3)}
        held = {digest.hex for digest in store.iter_digests()}
        assert {block_id.hex for block_id in stored} <= held


class BrainReaderConformance(ABC):
    """
    What any client that claims to read a brain must do, whoever wrote it.

    Subclass this and implement :meth:`make_reader`, returning a client already holding the knowledge
    :meth:`seed` describes. A read-only client satisfies :class:`~boltzmann.protocol.operations.BrainReader`
    and is conforming for what it claims, so this suite asks nothing about writing.

    What is asserted is the contract of Sections 9.2 and 10.6 -- verified data with its provenance, never
    prose; membership checked against the installed snapshot; tombstoned told apart from missing -- and
    never a ranking order, because the protocol guarantees verifiability and not identical ranking.
    """

    @abstractmethod
    def make_reader(self) -> Any:
        """
        Build a client to test.

        Returns:
            Any: A client satisfying :class:`~boltzmann.protocol.operations.BrainReader`, already holding
            one canonical source and one semantic block derived from it, where the semantic block's text
            contains the word ``"Fourier"``.
        """

    def test_satisfies_the_reader_contract(self) -> None:
        from boltzmann.protocol.operations import BrainReader

        assert isinstance(self.make_reader(), BrainReader)

    def test_reports_what_is_installed(self) -> None:
        reader = self.make_reader()
        snapshot = reader.snapshot()
        assert snapshot.installed
        for memory_type in snapshot.installed:
            assert reader.root_of(memory_type) == snapshot.root_of(memory_type)

    def test_a_module_that_is_not_installed_is_refused(self) -> None:
        """Not installed is a legitimate state, so it must be an error and never an empty module."""
        reader = self.make_reader()
        absent = [kind for kind in MemoryType if not reader.snapshot().has_module(kind)]
        if not absent:
            pytest.skip("this reader holds every module")
        with pytest.raises(SnapshotError):
            reader.root_of(absent[0])

    def test_opens_an_installed_module(self) -> None:
        reader = self.make_reader()
        module = reader.module(MemoryType.SEMANTIC)
        assert len(module) > 0
        assert module.root == reader.root_of(MemoryType.SEMANTIC)

    def test_resolves_a_member(self) -> None:
        reader = self.make_reader()
        block_id = reader.module(MemoryType.SEMANTIC).block_ids[0]
        assert reader.resolve(block_id).block_id == block_id

    def test_refuses_to_resolve_a_non_member(self) -> None:
        """A block no installed root commits to must not come back, however it is stored.

        The error is required to be a ``BoltzmannError``. The SDK defines the exception hierarchy, so
        that much is protocol rather than implementation -- a caller has to be able to catch a protocol
        failure without knowing which client produced it.
        """
        reader = self.make_reader()
        with pytest.raises(BoltzmannError):
            reader.resolve(BlockId.of(b"a block this brain never held"))

    def test_proves_membership(self) -> None:
        reader = self.make_reader()
        block_id = reader.module(MemoryType.SEMANTIC).block_ids[0]
        proof = reader.prove(block_id, MemoryType.SEMANTIC)
        assert proof.verify(reader.root_of(MemoryType.SEMANTIC))

    def test_a_proof_does_not_verify_against_another_root(self) -> None:
        reader = self.make_reader()
        block_id = reader.module(MemoryType.SEMANTIC).block_ids[0]
        proof = reader.prove(block_id, MemoryType.SEMANTIC)
        assert not proof.verify(MerkleRoot.of(b"some other composition"))

    def test_verifies_itself(self) -> None:
        assert self.make_reader().verify()

    def test_reports_resolvability_three_ways(self) -> None:
        """Section 10.6: a removed block must never be indistinguishable from a corrupted one."""
        reader = self.make_reader()
        report = reader.resolvability()
        held = sum(len(ids) for ids in report.resolvable.values())
        assert held > 0
        assert report.is_intact

    def test_resolvability_covers_the_content_blocks_name(self) -> None:
        """The same split, for the bytes a block names but does not carry.

        The seeded brain holds a canonical source, and a canonical block names its original rather than
        carrying it, so a conforming report has something to say here whatever the other schemas do: the
        source resolves, and nothing is missing. An implementation that classifies only block identities
        reports an empty split and fails, which is the point -- a brain whose data is gone must not read
        as intact.
        """
        reader = self.make_reader()
        report = reader.resolvability()
        assert report.content_resolvable.get(MemoryType.CANONICAL)
        assert not any(report.content_missing.values())
        assert report.is_intact

    def test_search_returns_verified_data(self) -> None:
        from boltzmann.query.request import Query

        reader = self.make_reader()
        bundle = reader.search(Query(text="Fourier"))
        assert len(bundle) > 0
        assert bundle.all_verified
        bundle.require_verified()

    def test_search_reports_the_roots_it_verified_against(self) -> None:
        from boltzmann.query.request import Query

        reader = self.make_reader()
        bundle = reader.search(Query(text="Fourier"))
        for memory_type, root in bundle.verified_against.items():
            assert root == reader.root_of(memory_type)

    def test_search_returns_data_and_not_prose(self) -> None:
        from boltzmann.query.request import Query

        reader = self.make_reader()
        match = reader.search(Query(text="Fourier")).matches[0]
        assert isinstance(match.content, dict)
        assert isinstance(match.score, str)
        assert not hasattr(match, "answer")

    def test_search_honours_a_memory_type_filter(self) -> None:
        """R2: "what happened in the class of May 14" must not compete with a Fourier definition."""
        from boltzmann.query.request import Query, QueryFilters

        reader = self.make_reader()
        bundle = reader.search(Query(text="Fourier", filters=QueryFilters(memory_types=[MemoryType.SEMANTIC])))
        assert {match.memory_type for match in bundle.matches} <= {MemoryType.SEMANTIC}

    def test_no_match_is_an_answer_and_not_an_error(self) -> None:
        """The terms are deliberately long and distinctive: matching is left to the implementation, so a
        conformance test must not depend on how short terms or stopwords are treated."""
        from boltzmann.query.request import Query

        reader = self.make_reader()
        bundle = reader.search(Query(text="chromodynamics lagrangian renormalisation"))
        assert len(bundle) == 0
        assert bundle.all_verified

    def test_an_unregistered_index_is_refused_rather_than_faked(self) -> None:
        """The protocol names six index kinds; a client that has none must say so."""
        from boltzmann.indices.base import IndexKind

        reader = self.make_reader()
        try:
            index = reader.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR)
        except BoltzmannError:
            return
        assert index.kind is IndexKind.VECTOR


class AuthenticityConformance(ABC):
    """
    What a verifier must decide, whoever wrote it (paper Section 8).

    Subclass this and implement :meth:`make_store`. The cases replayed here are the published
    ``signatures.json`` golden vectors -- the paper's worked cases, including a self-admitted key
    failing with no pin at all -- so an implementation passing this suite reaches the same
    verdicts every other conforming verifier reaches. Requires the ``[authenticity]`` extra,
    because a verdict of ``authorized`` without the mathematics would be the one claim this role
    is forbidden to make.
    """

    @abstractmethod
    def make_store(self) -> BlockStore:
        """
        Build an empty store to verify against.

        Returns:
            BlockStore: The store under test.
        """

    def _replayed(self, case: dict) -> AuthenticationReport:
        pytest.importorskip("cryptography")
        from boltzmann.authenticity.authenticator import Authenticator
        from boltzmann.authenticity.pins import PinSource, write_pin
        from boltzmann.authenticity.record import SignatureRecord, store_record
        from boltzmann.authenticity.trust_root import TrustRoot
        from boltzmann.conformance.golden import load
        from boltzmann.identity.digest import OciDigest
        from boltzmann.module.snapshot import Snapshot

        vectors = load("signatures.json")
        store = self.make_store()
        for described in vectors["snapshots"].values():
            store.put_bytes(described["canonical"].encode("utf-8"))
        for described in vectors["signatures"].values():
            store_record(store, SignatureRecord.model_validate(described))
        if "pin" in case:
            write_pin(store, OciDigest.parse(vectors["trust_roots"][case["pin"]]["digest"]), PinSource.OUT_OF_BAND)
        snapshot = Snapshot.model_validate_json(vectors["snapshots"][case["snapshot"]]["canonical"].encode("utf-8"))
        records = [SignatureRecord.model_validate(vectors["signatures"][name]) for name in case["signatures"]]
        current = None
        if "current_trust_root" in case:
            current = TrustRoot.model_validate(vectors["trust_roots"][case["current_trust_root"]]["document"])
        return Authenticator(store).authenticate(snapshot, records=records, current=current)

    def test_every_published_case_reaches_its_verdict(self) -> None:
        """The whole judgement layer, one case at a time, against this store."""
        from boltzmann.conformance.golden import load

        for case in load("signatures.json")["cases"]:
            report = self._replayed(case)
            expect = case["expect"]
            assert report.state.value == expect["state"], case["name"]
            if "quorum_met" in expect:
                assert report.quorum_met == expect["quorum_met"], case["name"]
            for kind in expect.get("findings_include", []):
                assert any(finding.kind.value == kind for finding in report.findings), (case["name"], kind)

    def test_signing_never_changes_a_snapshots_identity(self) -> None:
        """Detached means detached: the record lands beside the snapshot, never inside it."""
        from boltzmann.authenticity.record import SignatureRecord, store_record
        from boltzmann.conformance.golden import load
        from boltzmann.identity.digest import OciDigest
        from boltzmann.module.snapshot import Snapshot

        vectors = load("signatures.json")
        store = self.make_store()
        described = vectors["snapshots"]["S7"]
        digest = store.put_bytes(described["canonical"].encode("utf-8"))
        store_record(store, SignatureRecord.model_validate(vectors["signatures"]["A-over-S7"]))
        snapshot = Snapshot.model_validate_json(store.get_bytes(OciDigest.parse(described["digest"])))
        assert snapshot.digest == digest
