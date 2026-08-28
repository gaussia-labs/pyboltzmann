"""A module: blocks, the Merkle DAG that versions them, and its indices.

A module carries three responsibilities that coexist in the same physical unit
(paper Section 6): *blocks* contain knowledge, the *Merkle DAG* defines which blocks
form a version, and *indices* make it possible to find blocks without scanning all
of them.

This class is deliberately read-and-derive only. Reading verifies; deriving returns a
new module with a new root. Nothing here writes to a store, because the only write
path in the protocol is validate then commit -- see :mod:`boltzmann.ingest.commit`.
That is what keeps the design rule of Section 7.1 structural rather than advisory:
an external LLM holding a ``Module`` cannot mutate a Merkle DAG or an index with it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import MembershipError, MemoryTypeError
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.indices.base import Index
from boltzmann.merkle.diff import CompositionDiff
from boltzmann.merkle.proof import InclusionProof
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef
from boltzmann.store.base import BlockStore


class Module:
    """
    One memory module of a brain, at one version.

    Attributes:
        memory_type (MemoryType): Which of the five modules this is.
        composition (Composition): The set of blocks that form this version.
        store (BlockStore): Where the blocks' bytes live.
        indices (dict[str, Index]): Derived views over the composition.
    """

    def __init__(
        self,
        memory_type: MemoryType,
        store: BlockStore,
        composition: Composition | None = None,
        indices: dict[str, Index] | None = None,
    ) -> None:
        """
        Open a module over a store.

        Args:
            memory_type (MemoryType): Which module this is.
            store (BlockStore): Where the blocks' bytes live.
            composition (Composition | None): The version to open. Defaults to empty.
            indices (dict[str, Index] | None): Derived views, keyed by index name.

        Raises:
            ValueError: If ``composition`` belongs to a different module.
        """
        if composition is not None and composition.memory_type is not memory_type:
            raise ValueError(
                f"composition belongs to the {composition.memory_type.value} module, not {memory_type.value}"
            )
        self.memory_type = memory_type
        self.store = store
        self.composition = composition if composition is not None else Composition(memory_type)
        self.indices = dict(indices or {})

    # --- Identity -------------------------------------------------------------

    @property
    def root(self) -> MerkleRoot:
        """The Merkle root that identifies this version of the module."""
        return self.composition.root

    @property
    def block_ids(self) -> list[BlockId]:
        """The composition in canonical leaf order."""
        return self.composition.block_ids

    def persist(
        self,
        embedding_model: str | None = None,
        index_digest: OciDigest | None = None,
    ) -> ModuleRef:
        """
        Write this version's composition document and describe it for a snapshot.

        The document has to be stored for the version to be reopenable, so persisting it and
        producing the snapshot entry are one step: a ``ModuleRef`` that pointed at a document
        nobody wrote would name a version that cannot be recovered.

        Args:
            embedding_model (str | None): Model and version behind the vector index, when one
                travels with the module.
            index_digest (OciDigest | None): Content address of that index's exact serialized payload.

        Returns:
            ModuleRef: The snapshot entry for this module.
        """
        return ModuleRef(
            memory_type=self.memory_type,
            root=self.root,
            composition=self.store.put_bytes(self.composition.document()),
            block_count=len(self.composition),
            layout=self.composition.layout,
            embedding_model=embedding_model,
            index_digest=index_digest,
        )

    def __len__(self) -> int:
        return len(self.composition)

    def __contains__(self, block_id: object) -> bool:
        return block_id in self.composition

    def __repr__(self) -> str:
        return f"Module({self.memory_type.value}, n={len(self)}, root={self.root.short})"

    # --- Reading --------------------------------------------------------------

    def get(self, block_id: BlockId) -> Block:
        """
        Read a block, verifying that it belongs to this version.

        Membership is checked before the bytes are fetched. A block that exists in the
        store but is not in this composition was dropped, or belongs to another
        module: either way it is not part of what this root commits to, and returning
        it would break the guarantee that every result is verified against the
        installed snapshot (paper Section 9.2).

        Args:
            block_id (BlockId): The block to read.

        Returns:
            Block: The decoded block.

        Raises:
            MembershipError: If the block is not in this module's composition.
            MemoryTypeError: If the stored block's type contradicts this module.
        """
        if block_id not in self.composition:
            raise MembershipError(
                f"block {block_id.short} is not in the {self.memory_type.value} composition committed by "
                f"{self.root.short}"
            )
        block = self.store.get_block(block_id)
        if block.MEMORY_TYPE is not self.memory_type:
            raise MemoryTypeError(
                f"block {block_id.short} is a {block.MEMORY_TYPE.value} block but is held by the "
                f"{self.memory_type.value} module"
            )
        return block

    def blocks(self) -> Iterator[Block]:
        """
        Iterate every block of this version, in canonical leaf order.

        Returns:
            Iterator[Block]: The decoded blocks.
        """
        for block_id in self.composition:
            yield self.get(block_id)

    def schema_versions(self) -> tuple[int, ...]:
        """
        The distinct block schema versions this version holds, ascending.

        What a consumer needs in order to answer "can my SDK read this module?" without
        holding the module. Published on the artifact's manifest so the question can be
        answered before the download rather than after it.

        Returns:
            tuple[int, ...]: The versions present, or empty for an empty composition.

        Raises:
            BlockNotFoundError: If a block the composition names cannot be read. A version
                map that silently skipped unreadable blocks would understate what the module
                requires, which is the one way this could mislead a consumer.
        """
        return tuple(sorted({block.SCHEMA_VERSION for block in self.blocks()}))

    def inclusion_proof(self, block_id: BlockId) -> InclusionProof:
        """
        Prove that a block belongs to this version.

        Args:
            block_id (BlockId): The block whose membership is proven.

        Returns:
            InclusionProof: A proof of size ``O(log n)``.
        """
        return self.composition.inclusion_proof(block_id)

    def resolvable(self) -> dict[BlockId, bool]:
        """
        Which blocks of this version can still be read.

        A redacted block stays in the composition and still proves its membership,
        but its bytes are gone. Reporting the difference is required so that a removed
        block is never indistinguishable from a corrupted one (paper Section 10.6).

        Returns:
            dict[BlockId, bool]: Each block mapped to whether its bytes resolve.
        """
        return {block_id: self.store.is_resolvable(block_id) for block_id in self.composition}

    def verify(self) -> bool:
        """
        Check this version end to end: every membership proof, and every block's bytes.

        Returns:
            bool: Whether the composition is consistent and every resolvable block
            hashes to the identity it is filed under.
        """
        if not self.composition.verify():
            return False
        for block_id in self.composition:
            if not self.store.is_resolvable(block_id):
                continue
            if self.store.get_block(block_id).block_id != block_id:
                return False
        return True

    # --- Deriving -------------------------------------------------------------

    def with_blocks(self, block_ids: Iterable[BlockId]) -> Module:
        """
        Derive a version that also contains ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): Blocks to include.

        Returns:
            Module: The new version, sharing this module's store.
        """
        return Module(self.memory_type, self.store, self.composition.add(block_ids), self.indices)

    def without_blocks(self, block_ids: Iterable[BlockId]) -> Module:
        """
        Derive a version that excludes ``block_ids``.

        Args:
            block_ids (Iterable[BlockId]): Blocks to exclude.

        Returns:
            Module: The new version, sharing this module's store.

        Raises:
            AppendOnlyViolationError: If this module is append-only.
        """
        return Module(self.memory_type, self.store, self.composition.drop(block_ids), self.indices)

    def diff(self, other: Module) -> CompositionDiff:
        """
        Compare this version with a later one.

        Args:
            other (Module): The later version of the same module.

        Returns:
            CompositionDiff: What was added, removed, and shared.
        """
        return self.composition.diff(other.composition)
