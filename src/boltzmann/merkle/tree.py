"""The internal Merkle DAG: RFC 9162 over lexicographically sorted leaves.

On their own, blocks are a bag of content-addressed units. The Merkle root names,
verifies, and diffs a whole *version* of a module: that root **is** the identity of
the version (paper Section 6.2).

**Why RFC 9162.** A naive binary tree that duplicates the last node on an odd level
admits a second-preimage attack: two different leaf sets can produce the same root
(CVE-2012-2459). The Merkle Tree Hash of RFC 9162, Section 2.1.1, splits at the
largest power of two below ``n`` instead, which is unambiguous, and prefixes leaves
and internal nodes differently so a leaf hash can never be mistaken for a node hash.

RFC 9162 obsoletes RFC 6962, which is what these citations used to point at, and it
defines the same tree: same empty hash, same ``0x00``/``0x01`` prefixes, same split.
Nothing computed here changed when the citations moved -- see :data:`LAYOUT_NAME`.

**Why sorted leaves.** Sorting makes the root a pure function of the *set* of
blocks, which is exactly what the paper claims in Section 6.2: two parties that
assembled the same blocks obtain the same root. A layout that preserved insertion
order would break that.

**What is persisted.** Only the sorted leaf list and the root. Internal nodes are
32 bytes of scaffolding derived on demand, so there is nothing to keep in sync.
This is also why differencing two versions is a set operation over leaf lists
rather than a descent through stored nodes -- see :mod:`boltzmann.merkle.diff`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.exceptions import MerkleError
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.identity.hashing import hash_empty, hash_leaf, hash_node
from boltzmann.merkle.proof import InclusionProof

if TYPE_CHECKING:
    from collections.abc import Iterable

LAYOUT_NAME = "rfc6962-sorted/1"
"""Identifier of this layout, recorded alongside a snapshot.

Keeps the historical name deliberately, now that the citations say RFC 9162. The string
names a *construction*, and RFC 9162 defines the same construction RFC 6962 did, so a root
computed under either reading is byte-identical. Renaming it would announce a change of
tree where none happened -- and it would be a hard break, not a cosmetic one:
:meth:`~boltzmann.module.composition.Composition.from_document` refuses a composition whose
layout it does not implement, so every brain already published would stop opening, and the
published golden vectors record this string.

The version suffix is what moves if the construction ever does.
"""


def sorted_leaves(block_ids: Iterable[BlockId]) -> list[BlockId]:
    """
    Normalize a composition into the canonical leaf order.

    Duplicates collapse, because a set of content-addressed blocks cannot hold the
    same block twice.

    Args:
        block_ids (Iterable[BlockId]): The blocks in a module's composition.

    Returns:
        list[BlockId]: The blocks, deduplicated and ordered by digest.
    """
    return sorted(set(block_ids), key=lambda block_id: block_id.raw)


def _largest_power_of_two_below(n: int) -> int:
    """The largest power of two strictly smaller than ``n``, for ``n > 1``."""
    return 1 << ((n - 1).bit_length() - 1)


class MerkleTree:
    """
    The Merkle commitment over one module's composition.

    Attributes:
        leaves (list[BlockId]): The composition, in canonical leaf order.
    """

    def __init__(self, block_ids: Iterable[BlockId]) -> None:
        """
        Build the commitment for a composition.

        Args:
            block_ids (Iterable[BlockId]): The blocks that make up the version.
        """
        self.leaves = sorted_leaves(block_ids)
        self._raw = [block_id.raw for block_id in self.leaves]
        self._members = frozenset(self.leaves)
        self._index = {block_id: position for position, block_id in enumerate(self.leaves)}
        self._nodes: dict[tuple[int, int], bytes] = {}
        """Internal node hashes, keyed by the span they cover.

        Nothing here is persisted -- a stored composition is still just the leaf list, and these
        are still 32 bytes of scaffolding derived on demand. What changed is that they are derived
        *once* per tree rather than once per question. Every proof asks for sibling subtree hashes,
        and recomputing each from the leaves made a whole-composition ``verify`` quadratic: at 2000
        blocks it took four seconds, and doubling the module quadrupled that. The recursion only
        ever visits the O(n) spans the canonical split produces, so this cache is linear in the
        composition and turns ``verify`` into O(n log n).
        """

    def _subtree_hash(self, start: int, end: int) -> bytes:
        """``MTH(D[start:end])``, computed once and remembered."""
        cached = self._nodes.get((start, end))
        if cached is not None:
            return cached

        if end - start == 1:
            computed = hash_leaf(self._raw[start])
        else:
            split = start + _largest_power_of_two_below(end - start)
            computed = hash_node(self._subtree_hash(start, split), self._subtree_hash(split, end))

        self._nodes[(start, end)] = computed
        return computed

    def __len__(self) -> int:
        return len(self.leaves)

    def __contains__(self, block_id: object) -> bool:
        return block_id in self._members

    @property
    def name(self) -> str:
        """Identifier of this layout."""
        return LAYOUT_NAME

    @property
    def root(self) -> MerkleRoot:
        """
        The root that commits to this composition.

        An empty composition hashes to ``SHA-256("")``, which is ``MTH({})`` in RFC 9162,
        Section 2.1.1: a module with no blocks still has a well-defined identity.
        """
        if not self._raw:
            return MerkleRoot.from_raw(hash_empty())
        return MerkleRoot.from_raw(self._subtree_hash(0, len(self._raw)))

    def index_of(self, block_id: BlockId) -> int:
        """
        Position of a block among the sorted leaves.

        Args:
            block_id (BlockId): The block to locate.

        Returns:
            int: Its leaf index.

        Raises:
            MerkleError: If the block is not part of this composition.
        """
        try:
            return self._index[block_id]
        except KeyError:
            raise MerkleError(f"block {block_id.short} is not in this composition") from None

    def inclusion_proof(self, block_id: BlockId) -> InclusionProof:
        """
        Build a proof that ``block_id`` belongs to this composition.

        Args:
            block_id (BlockId): The block whose membership is proven.

        Returns:
            InclusionProof: The sibling hashes along the path to the root.

        Raises:
            MerkleError: If the block is not part of this composition.
        """
        index = self.index_of(block_id)
        path: list[str] = []
        self._collect_path(index, 0, len(self._raw), path)
        return InclusionProof(
            block_id=block_id,
            leaf_index=index,
            tree_size=len(self._raw),
            audit_path=path,
        )

    def _collect_path(self, index: int, start: int, end: int, path: list[str]) -> None:
        """Walk down to the leaf, recording the sibling subtree hash at each level."""
        if end - start == 1:
            return
        split = start + _largest_power_of_two_below(end - start)
        if start + index < split:
            self._collect_path(index, start, split, path)
            path.append(self._subtree_hash(split, end).hex())
        else:
            self._collect_path(start + index - split, split, end, path)
            path.append(self._subtree_hash(start, split).hex())

    def verify(self) -> bool:
        """
        Recompute every leaf's proof against the root.

        Integrity is checked by recomputing hashes from the leaves up and comparing
        the root (paper Section 6.2). This is the expensive, whole-module form of
        that check; :meth:`InclusionProof.verify` is the cheap per-block form.

        Returns:
            bool: Whether every leaf proves into the root.
        """
        root = self.root
        return all(self.inclusion_proof(leaf).verify(root) for leaf in self.leaves)


class SortedRfc9162Layout:
    """The default :class:`~boltzmann.merkle.layout.MerkleLayout`."""

    @property
    def name(self) -> str:
        """Identifier of this layout."""
        return LAYOUT_NAME

    def root(self, block_ids: Iterable[BlockId]) -> MerkleRoot:
        """
        Compute the root that commits to ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): The composition of a module.

        Returns:
            MerkleRoot: The identity of that composition.
        """
        return MerkleTree(block_ids).root

    def inclusion_proof(self, block_ids: Iterable[BlockId], target: BlockId) -> InclusionProof:
        """
        Build a proof that ``target`` belongs to ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): The composition of a module.
            target (BlockId): The block whose membership is proven.

        Returns:
            InclusionProof: The proof.
        """
        return MerkleTree(block_ids).inclusion_proof(target)


DEFAULT_LAYOUT = SortedRfc9162Layout()
"""The layout every module uses unless an implementation substitutes another."""


def merkle_root(block_ids: Iterable[BlockId]) -> MerkleRoot:
    """
    Compute the Merkle root of a composition with the default layout.

    Args:
        block_ids (Iterable[BlockId]): The composition of a module.

    Returns:
        MerkleRoot: The identity of that composition.
    """
    return MerkleTree(block_ids).root
