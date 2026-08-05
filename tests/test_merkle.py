"""Properties of the Merkle layout, plus an independent check of the hashing itself."""

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import InclusionProofError, MerkleError
from boltzmann.identity.digest import BlockId
from boltzmann.merkle.diff import diff
from boltzmann.merkle.layout import MerkleLayout
from boltzmann.merkle.proof import is_node_hash
from boltzmann.merkle.tree import DEFAULT_LAYOUT, MerkleTree, merkle_root, sorted_leaves
from boltzmann.module.composition import Composition

block_ids = st.builds(BlockId.of, st.binary(min_size=1, max_size=32))
compositions = st.lists(block_ids, min_size=1, max_size=24, unique_by=lambda value: value.hex)


def leaf(digest: bytes) -> bytes:
    """Hash a leaf the way RFC 9162 does, computed independently of the implementation."""
    return hashlib.sha256(b"\x00" + digest).digest()


def node(left: bytes, right: bytes) -> bytes:
    """Hash an internal node the way RFC 9162 does, computed independently."""
    return hashlib.sha256(b"\x01" + left + right).digest()


class TestRootIsFunctionOfTheSet:
    """Two parties that assembled the same blocks must obtain the same root."""

    @given(compositions)
    def test_order_does_not_matter(self, blocks: list[BlockId]) -> None:
        assert merkle_root(blocks) == merkle_root(list(reversed(blocks)))

    @given(compositions, st.randoms())
    def test_any_permutation_gives_the_same_root(self, blocks: list[BlockId], rng) -> None:
        shuffled = blocks[:]
        rng.shuffle(shuffled)
        assert merkle_root(shuffled) == merkle_root(blocks)

    @given(compositions)
    def test_duplicates_collapse(self, blocks: list[BlockId]) -> None:
        assert merkle_root(blocks + blocks) == merkle_root(blocks)

    @given(compositions)
    def test_leaves_are_sorted_and_unique(self, blocks: list[BlockId]) -> None:
        leaves = sorted_leaves(blocks)
        assert leaves == sorted(set(blocks), key=lambda value: value.raw)

    @given(compositions, block_ids)
    def test_adding_a_block_changes_the_root(self, blocks: list[BlockId], extra: BlockId) -> None:
        if extra in blocks:
            return
        assert merkle_root([*blocks, extra]) != merkle_root(blocks)


class TestInclusionProofs:
    """Membership must be provable, and only for blocks that are actually members."""

    @given(compositions)
    def test_every_leaf_proves_into_the_root(self, blocks: list[BlockId]) -> None:
        tree = MerkleTree(blocks)
        root = tree.root
        for block in tree.leaves:
            assert tree.inclusion_proof(block).verify(root)

    @given(compositions)
    def test_proof_size_is_logarithmic(self, blocks: list[BlockId]) -> None:
        tree = MerkleTree(blocks)
        ceiling = max(1, len(tree)).bit_length()
        for block in tree.leaves:
            assert len(tree.inclusion_proof(block).audit_path) <= ceiling

    @given(compositions)
    def test_audit_path_entries_are_node_hashes(self, blocks: list[BlockId]) -> None:
        tree = MerkleTree(blocks)
        for block in tree.leaves:
            assert all(is_node_hash(entry) for entry in tree.inclusion_proof(block).audit_path)

    @given(st.lists(block_ids, min_size=2, max_size=16, unique_by=lambda value: value.hex))
    def test_a_tampered_path_does_not_verify(self, blocks: list[BlockId]) -> None:
        tree = MerkleTree(blocks)
        proof = tree.inclusion_proof(tree.leaves[0])
        if not proof.audit_path:
            return
        flipped = list(proof.audit_path)
        flipped[0] = f"{'0' if flipped[0][0] != '0' else '1'}{flipped[0][1:]}"
        assert not proof.model_copy(update={"audit_path": flipped}).verify(tree.root)

    @given(st.lists(block_ids, min_size=2, max_size=16, unique_by=lambda value: value.hex))
    def test_a_proof_from_another_composition_does_not_verify(self, blocks: list[BlockId]) -> None:
        tree = MerkleTree(blocks)
        proof = tree.inclusion_proof(tree.leaves[0])
        assert not proof.verify(MerkleTree(blocks[1:]).root)

    def test_require_raises_on_failure(self) -> None:
        blocks = [BlockId.of(f"b{index}".encode()) for index in range(4)]
        tree = MerkleTree(blocks)
        proof = tree.inclusion_proof(blocks[0])
        proof.require(tree.root)
        with pytest.raises(InclusionProofError):
            proof.require(MerkleTree(blocks[1:]).root)

    def test_proving_a_non_member_raises(self) -> None:
        tree = MerkleTree([BlockId.of(b"member")])
        with pytest.raises(MerkleError, match="not in this composition"):
            tree.inclusion_proof(BlockId.of(b"stranger"))


class TestHashingAgainstRfc9162:
    """Cross-check the roots against hashing computed by hand, not by the implementation."""

    def test_empty_tree(self) -> None:
        assert merkle_root([]).raw == hashlib.sha256(b"").digest()

    def test_sizes_one_to_five(self) -> None:
        blocks = sorted_leaves(BlockId.of(f"b{index}".encode()) for index in range(5))
        raw = [block.raw for block in blocks]
        expected = {
            1: leaf(raw[0]),
            2: node(leaf(raw[0]), leaf(raw[1])),
            3: node(node(leaf(raw[0]), leaf(raw[1])), leaf(raw[2])),
            4: node(node(leaf(raw[0]), leaf(raw[1])), node(leaf(raw[2]), leaf(raw[3]))),
            5: node(node(node(leaf(raw[0]), leaf(raw[1])), node(leaf(raw[2]), leaf(raw[3]))), leaf(raw[4])),
        }
        for size, root in expected.items():
            assert merkle_root(blocks[:size]).raw == root, f"size {size}"

    def test_leaf_and_node_hashes_are_domain_separated(self) -> None:
        """A single leaf's root must not equal the bare digest, or a leaf could pose as a node."""
        block = BlockId.of(b"only")
        assert merkle_root([block]).raw != block.raw

    def test_verify_recomputes_every_proof(self) -> None:
        for size in range(1, 40):
            blocks = [BlockId.of(f"x{index}".encode()) for index in range(size)]
            assert MerkleTree(blocks).verify(), f"size {size}"


class TestDiff:
    """Differencing must report exactly what an incremental update has to transfer."""

    @given(compositions, compositions)
    def test_matches_set_difference(self, before: list[BlockId], after: list[BlockId]) -> None:
        result = diff(before, after)
        assert set(result.added) == set(after) - set(before)
        assert set(result.removed) == set(before) - set(after)
        assert set(result.unchanged) == set(before) & set(after)

    @given(compositions)
    def test_identical_compositions_diff_to_nothing(self, blocks: list[BlockId]) -> None:
        result = diff(blocks, blocks)
        assert result.is_empty
        assert result.before == result.after
        assert result.transfer_size == 0

    @given(compositions, compositions)
    def test_roots_match_the_compositions(self, before: list[BlockId], after: list[BlockId]) -> None:
        result = diff(before, after)
        assert result.before == merkle_root(before)
        assert result.after == merkle_root(after)

    @given(compositions)
    def test_transfer_size_counts_only_additions(self, blocks: list[BlockId]) -> None:
        extra = BlockId.of(b"a block that is certainly new")
        if extra in blocks:
            return
        assert diff(blocks, [*blocks, extra]).transfer_size == 1


class TestLayout:
    """The default layout must satisfy the interface the protocol depends on."""

    def test_default_layout_satisfies_the_protocol(self) -> None:
        assert isinstance(DEFAULT_LAYOUT, MerkleLayout)

    @given(compositions)
    def test_layout_agrees_with_the_tree(self, blocks: list[BlockId]) -> None:
        tree = MerkleTree(blocks)
        assert DEFAULT_LAYOUT.root(blocks) == tree.root
        target = tree.leaves[0]
        assert DEFAULT_LAYOUT.inclusion_proof(blocks, target) == tree.inclusion_proof(target)

    def test_layout_is_named(self) -> None:
        assert DEFAULT_LAYOUT.name == "rfc6962-sorted/1"

    def test_the_identifier_keeps_the_historical_name(self) -> None:
        """Not an oversight when the citations say RFC 9162, and not safe to tidy.

        RFC 9162 obsoletes RFC 6962 and defines the same construction, so nothing computed here moved
        when the references did. The string travels inside the composition document, which is hashed and
        published, and ``Composition.from_document`` refuses a layout it does not implement -- so
        renaming it would make every brain already published unopenable, to say that a tree changed when
        it did not. The ``/1`` suffix is what moves if the construction ever does.
        """
        assert "9162" not in DEFAULT_LAYOUT.name
        assert Composition(MemoryType.CANONICAL, []).layout == "rfc6962-sorted/1"
        assert b'"layout":"rfc6962-sorted/1"' in Composition(MemoryType.CANONICAL, []).document()
