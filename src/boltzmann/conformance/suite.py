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

import pytest

from boltzmann.blocks.canonical import CanonicalBlock
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import SemanticBlock, SemanticKind
from boltzmann.conformance import golden
from boltzmann.exceptions import (
    AppendOnlyViolationError,
    BlockIntegrityError,
    BlockNotFoundError,
    BlockTombstonedError,
    DigestKindError,
    NonDeterministicValueError,
)
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize
from boltzmann.merkle.tree import MerkleTree, merkle_root
from boltzmann.module.composition import Composition

if TYPE_CHECKING:
    from boltzmann.store.base import BlockStore


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
        """Every block schema survives the store."""
        store = self.make_store()
        for block in (sample_semantic(), sample_canonical()):
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
