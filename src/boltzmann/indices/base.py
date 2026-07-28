"""Indices are derived views, never the source of truth.

Knowledge lives in blocks; indices exist so that finding a block does not require
scanning all of them. They may travel inside a module so a consumer need not
recompute them, but they never replace the content (paper Section 6.3).

Whether an index can travel or must be rebuilt follows from one question: can it be
regenerated without a model? Structural indices -- hash map, B-tree, inverted,
bitmap, and the graph when relations live on the blocks -- are deterministic functions
of the composition and any client can rebuild them cheaply. The vector index is the
exception: rebuilding it requires an embedding model, which a model-agnostic client
does not carry, so it travels with the module and records the model that produced it.

:attr:`Index.rebuildable` is that distinction made explicit, so a client can decide
what it must download versus what it can regenerate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.blocks.base import Block
    from boltzmann.identity.digest import BlockId


class IndexKind(StrEnum):
    """The index kinds the protocol names, and the query shape each serves.

    Selection follows the shape of the query: exact identifiers resolve through the
    hash map, ordered or range predicates through the B-tree, term matches through the
    inverted index, natural-language intent through the vector index, neighborhood and
    traversal through the graph, and categorical filters through bitmaps
    (paper Section 9.2).
    """

    HASH_MAP = "hash_map"
    BTREE = "btree"
    INVERTED = "inverted"
    VECTOR = "vector"
    GRAPH = "graph"
    BITMAP = "bitmap"


@runtime_checkable
class Index(Protocol):
    """A derived view over a module's composition."""

    @property
    def kind(self) -> IndexKind:
        """Which kind of index this is."""
        ...

    @property
    def rebuildable(self) -> bool:
        """
        Whether any client can regenerate this index from the blocks alone.

        ``False`` means the index must travel with the module, and that
        :attr:`model_tag` says what produced it.
        """
        ...

    @property
    def model_tag(self) -> str | None:
        """The model and version behind this index, for indices that need one."""
        ...

    def build(self, blocks: Iterable[Block]) -> None:
        """
        Populate the index from a composition.

        Args:
            blocks (Iterable[Block]): The blocks of the version being indexed.
        """
        ...

    def search(self, query: Any, limit: int = 10) -> list[tuple[BlockId, float]]:
        """
        Return candidate blocks with a retrieval score.

        An index returns candidates, never an answer, and never the authoritative
        result: no single index may be treated as authoritative (paper Section 9.2).

        Args:
            query (Any): A query in whatever form this index kind accepts.
            limit (int): Maximum number of candidates.

        Returns:
            list[tuple[BlockId, float]]: Candidates paired with their score.
        """
        ...


class AbstractIndex(ABC):
    """
    Base for the indices this SDK ships.

    Subclasses declare their kind and implement building and searching. Third-party
    indices only need to satisfy :class:`Index`.
    """

    KIND: ClassVar[IndexKind]
    REBUILDABLE: ClassVar[bool] = True

    @property
    def kind(self) -> IndexKind:
        """Which kind of index this is."""
        return self.KIND

    @property
    def rebuildable(self) -> bool:
        """Whether any client can regenerate this index from the blocks alone."""
        return self.REBUILDABLE

    @property
    def model_tag(self) -> str | None:
        """The model and version behind this index. ``None`` for structural indices."""
        return None

    @abstractmethod
    def build(self, blocks: Iterable[Block]) -> None:
        """Populate the index from a composition."""

    @abstractmethod
    def search(self, query: Any, limit: int = 10) -> list[tuple[BlockId, float]]:
        """Return candidate blocks with a retrieval score."""
