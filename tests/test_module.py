"""Compositions, modules, and snapshots: what a version of a brain is."""

import pytest
from pydantic import ValidationError

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import SemanticBlock, SemanticKind
from boltzmann.exceptions import (
    AppendOnlyViolationError,
    MembershipError,
    MemoryTypeError,
    ModuleError,
    SnapshotError,
)
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.merkle.tree import LAYOUT_NAME
from boltzmann.module.composition import Composition
from boltzmann.module.module import Module
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.store.memory import MemoryBlockStore


def block(label: str) -> SemanticBlock:
    return SemanticBlock(kind=SemanticKind.CONCEPT, label=label, statement=f"about {label}")


@pytest.fixture
def blocks() -> list[SemanticBlock]:
    return [block(f"concept {index}") for index in range(4)]


@pytest.fixture
def store(blocks: list[SemanticBlock]) -> MemoryBlockStore:
    store = MemoryBlockStore()
    for item in blocks:
        store.put_block(item)
    return store


class TestComposition:
    """A composition is immutable; deriving one yields a new root."""

    def test_is_deduplicated_and_ordered(self, blocks: list[SemanticBlock]) -> None:
        ids = [item.block_id for item in blocks]
        composition = Composition(MemoryType.SEMANTIC, [*ids, *ids])
        assert len(composition) == len(ids)
        assert composition.block_ids == sorted(ids, key=lambda value: value.raw)

    def test_add_and_drop_do_not_mutate(self, blocks: list[SemanticBlock]) -> None:
        ids = [item.block_id for item in blocks]
        composition = Composition(MemoryType.SEMANTIC, ids)
        root = composition.root
        composition.drop([ids[0]])
        composition.add([BlockId.of(b"new")])
        assert composition.root == root
        assert len(composition) == len(ids)

    def test_records_its_layout(self, blocks: list[SemanticBlock]) -> None:
        assert Composition(MemoryType.SEMANTIC, [blocks[0].block_id]).layout == LAYOUT_NAME

    def test_episodic_refuses_drop(self) -> None:
        target = BlockId.of(b"an episode")
        with pytest.raises(AppendOnlyViolationError, match="append-only"):
            Composition(MemoryType.EPISODIC, [target]).drop([target])

    @pytest.mark.parametrize(
        "memory_type",
        [MemoryType.CANONICAL, MemoryType.SEMANTIC, MemoryType.PROCEDURAL, MemoryType.PROVENANCE],
    )
    def test_non_episodic_modules_permit_drop(self, memory_type: MemoryType) -> None:
        target = BlockId.of(b"a block")
        assert len(Composition(memory_type, [target]).drop([target])) == 0

    def test_diff_across_modules_is_refused(self) -> None:
        target = BlockId.of(b"a block")
        with pytest.raises(ValueError, match="cannot diff"):
            Composition(MemoryType.SEMANTIC, [target]).diff(Composition(MemoryType.PROCEDURAL, [target]))

    def test_equality_accounts_for_the_module(self) -> None:
        target = BlockId.of(b"a block")
        assert Composition(MemoryType.SEMANTIC, [target]) == Composition(MemoryType.SEMANTIC, [target])
        assert Composition(MemoryType.SEMANTIC, [target]) != Composition(MemoryType.PROCEDURAL, [target])


class TestModule:
    """Reading verifies; deriving returns a new module."""

    def test_reads_its_blocks(self, store: MemoryBlockStore, blocks: list[SemanticBlock]) -> None:
        module = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [b.block_id for b in blocks]))
        assert {item.block_id for item in module.blocks()} == {item.block_id for item in blocks}

    def test_refuses_a_block_outside_its_composition(
        self, store: MemoryBlockStore, blocks: list[SemanticBlock]
    ) -> None:
        """A dropped block is still in the store, but not in what this root commits to."""
        composition = Composition(MemoryType.SEMANTIC, [item.block_id for item in blocks[:2]])
        module = Module(MemoryType.SEMANTIC, store, composition)
        assert store.has(blocks[3].block_id)
        with pytest.raises(MembershipError):
            module.get(blocks[3].block_id)

    def test_refuses_a_block_of_the_wrong_type(self, store: MemoryBlockStore, blocks: list[SemanticBlock]) -> None:
        composition = Composition(MemoryType.PROCEDURAL, [blocks[0].block_id])
        module = Module(MemoryType.PROCEDURAL, store, composition)
        with pytest.raises(MemoryTypeError, match="semantic block"):
            module.get(blocks[0].block_id)

    def test_composition_must_match_the_module(self, store: MemoryBlockStore) -> None:
        with pytest.raises(ValueError, match="belongs to the"):
            Module(MemoryType.SEMANTIC, store, Composition(MemoryType.PROCEDURAL))

    def test_verifies_end_to_end(self, store: MemoryBlockStore, blocks: list[SemanticBlock]) -> None:
        module = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [b.block_id for b in blocks]))
        assert module.verify()

    def test_verification_survives_a_tombstone(self, store: MemoryBlockStore, blocks: list[SemanticBlock]) -> None:
        """Redaction removes bytes but not membership, so the composition still verifies."""
        module = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [b.block_id for b in blocks]))
        store.tombstone(blocks[0].block_id, "erasure policy")
        assert module.verify()
        assert module.resolvable()[blocks[0].block_id] is False
        assert module.inclusion_proof(blocks[0].block_id).verify(module.root)

    def test_deriving_shares_the_store(self, store: MemoryBlockStore, blocks: list[SemanticBlock]) -> None:
        module = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [b.block_id for b in blocks]))
        reduced = module.without_blocks([blocks[0].block_id])
        assert reduced.store is module.store
        assert reduced.root != module.root
        assert module.diff(reduced).removed == [blocks[0].block_id]

    def test_persist_writes_the_composition_and_describes_the_version(
        self, store: MemoryBlockStore, blocks: list[SemanticBlock]
    ) -> None:
        module = Module(MemoryType.SEMANTIC, store, Composition(MemoryType.SEMANTIC, [b.block_id for b in blocks]))
        reference = module.persist(embedding_model="qwen3-embedding@1.0")
        assert reference.root == module.root
        assert reference.block_count == len(blocks)
        assert reference.layout == LAYOUT_NAME
        assert reference.embedding_model == "qwen3-embedding@1.0"

        recovered = Composition.from_document(store.get_bytes(reference.composition))
        assert recovered == module.composition
        assert recovered.root == reference.root

    def test_a_composition_document_round_trips(self, store: MemoryBlockStore, blocks: list[SemanticBlock]) -> None:
        """A root can be verified but not inverted, so the leaf list has to be stored."""
        composition = Composition(MemoryType.SEMANTIC, [b.block_id for b in blocks])
        assert Composition.from_document(composition.document()) == composition

    def test_an_empty_composition_document_round_trips(self) -> None:
        composition = Composition(MemoryType.PROCEDURAL)
        assert Composition.from_document(composition.document()) == composition

    @pytest.mark.parametrize(
        ("mutation", "match"),
        [
            (lambda doc: b"not json", "not valid JSON"),
            (lambda doc: b"[]", "must be an object"),
            (lambda doc: doc.replace(b'"boltzmann":1', b'"boltzmann":99'), "protocol version"),
            (lambda doc: doc.replace(b'"semantic"', b'"mythical"'), "unknown memory type"),
            (lambda doc: doc.replace(b"rfc6962-sorted/1", b"prolly/1"), "Merkle layout"),
        ],
    )
    def test_a_malformed_composition_document_is_refused(self, mutation, match: str) -> None:
        document = Composition(MemoryType.SEMANTIC, [BlockId.of(b"a block")]).document()
        with pytest.raises(ModuleError, match=match):
            Composition.from_document(mutation(document))


class TestSnapshot:
    """A snapshot names one root per installed module, and chains to its predecessor."""

    def build(self, *memory_types: MemoryType) -> Snapshot:
        return Snapshot.of(
            ModuleRef(
                memory_type=kind,
                root=MerkleRoot.of(kind.value.encode()),
                composition=OciDigest.of(kind.value.encode()),
                block_count=1,
            )
            for kind in memory_types
        )

    def test_reports_what_is_installed(self) -> None:
        snapshot = self.build(MemoryType.SEMANTIC, MemoryType.CANONICAL)
        assert snapshot.installed == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert snapshot.has_module(MemoryType.SEMANTIC)
        assert not snapshot.has_module(MemoryType.EPISODIC)

    def test_a_partial_install_is_valid(self) -> None:
        """Selective installation is the point of packaging each module separately."""
        snapshot = self.build(MemoryType.EPISODIC)
        assert snapshot.installed == [MemoryType.EPISODIC]
        assert snapshot.block_count == 1

    def test_missing_module_raises_with_what_is_installed(self) -> None:
        snapshot = self.build(MemoryType.SEMANTIC)
        with pytest.raises(SnapshotError, match="installed: semantic"):
            snapshot.root_of(MemoryType.PROCEDURAL)

    def test_advancing_one_module_leaves_the_others_alone(self) -> None:
        """This is what makes an incremental update cheap."""
        snapshot = self.build(MemoryType.CANONICAL, MemoryType.SEMANTIC)
        advanced = snapshot.with_module(
            ModuleRef(
                memory_type=MemoryType.SEMANTIC,
                root=MerkleRoot.of(b"semantic v2"),
                composition=OciDigest.of(b"semantic v2 leaves"),
                block_count=2,
            )
        )
        assert advanced.root_of(MemoryType.CANONICAL) == snapshot.root_of(MemoryType.CANONICAL)
        assert advanced.root_of(MemoryType.SEMANTIC) != snapshot.root_of(MemoryType.SEMANTIC)

    def test_successors_chain_by_digest(self) -> None:
        snapshot = self.build(MemoryType.SEMANTIC)
        advanced = snapshot.with_module(
            ModuleRef(
                memory_type=MemoryType.SEMANTIC,
                root=MerkleRoot.of(b"v2"),
                composition=OciDigest.of(b"v2 leaves"),
                block_count=2,
            )
        )
        assert advanced.parent == snapshot.digest

    def test_uninstalling_a_module(self) -> None:
        snapshot = self.build(MemoryType.CANONICAL, MemoryType.SEMANTIC)
        reduced = snapshot.without_module(MemoryType.SEMANTIC)
        assert reduced.installed == [MemoryType.CANONICAL]

    def test_digest_is_a_physical_identity(self) -> None:
        """A snapshot document is a transportable file, so its own digest is an OciDigest."""
        snapshot = self.build(MemoryType.SEMANTIC)
        assert snapshot.digest.KIND == "oci_digest"

    def test_serializes_to_canonical_bytes(self) -> None:
        snapshot = self.build(MemoryType.SEMANTIC)
        assert Snapshot.model_validate_json(snapshot.canonical_bytes()) == snapshot

    def test_roots_are_merkle_roots_not_strings(self) -> None:
        with pytest.raises(ValidationError):
            ModuleRef(
                memory_type=MemoryType.SEMANTIC,
                root=BlockId.of(b"a block"),
                composition=OciDigest.of(b"leaves"),
                block_count=1,
            )
