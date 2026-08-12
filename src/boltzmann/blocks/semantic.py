"""Semantic memory: what general knowledge was consolidated (paper Section 5).

Holds concepts, formulas, facts, relations, and constraints, linked to the
canonical sources they were derived from.

The name is a coincidence worth stating: *semantic* here denotes general,
consolidated knowledge as opposed to episodic memory. It does not mean an
embedding-based representation. A block carries meaning in two portable,
**symbolic** forms -- its text and its explicit relations to other blocks -- while
learned, sub-symbolic representations live only in the derived vector index
(paper Section 6.3).
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.content import NamesContent
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId


class SemanticKind(StrEnum):
    """What kind of consolidated knowledge a semantic block states."""

    CONCEPT = "concept"
    FACT = "fact"
    FORMULA = "formula"
    RELATION = "relation"
    CONSTRAINT = "constraint"


class Relation(BaseModel):
    """
    An explicit, symbolic edge from this block to another.

    In aggregate these edges form the knowledge graph, which is why they live on
    the block and not only in the derived graph index: the index can be rebuilt
    from them, but they cannot be rebuilt from the index.

    Attributes:
        predicate (str): What the edge asserts, such as ``depends_on`` or ``part_of``.
        target (BlockId): The block the edge points at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicate: str = Field(min_length=1)
    target: BlockId


class SemanticBlock(Block):
    """
    A unit of consolidated general knowledge.

    Attributes:
        kind (SemanticKind): Which kind of statement this is.
        label (str): Short name the knowledge is known by.
        statement (str): The knowledge itself.
        subject (str | None): Domain the knowledge belongs to, for filtering.
        evidence (list[BlockId] | None): Canonical blocks this interpretation cites.
            A canonical drop cascades to every block that lists it here.
        relations (list[Relation] | None): Explicit edges to other blocks.
        aliases (list[str] | None): Other names for the same knowledge, used for
            identity resolution and deduplication.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.SEMANTIC

    kind: SemanticKind
    label: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    subject: str | None = None
    evidence: list[BlockId] | None = None
    relations: list[Relation] | None = None
    aliases: list[str] | None = None


class SemanticBlockV2(NamesContent, SemanticBlock):
    """
    Consolidated knowledge whose datum may be a file rather than a sentence.

    An interpretation of an image is still an interpretation: it belongs in semantic memory,
    it cites the canonical evidence it was derived from, and it is not itself evidence. What
    v1 could not express is that the *thing being asserted* is sometimes not text -- a
    rendered diagram, an extracted figure, a plotted curve -- and inlining that into a JSON
    payload that is canonically serialized and hashed on every access is not an option.

    **``statement`` stays required.** A block carries meaning in portable, symbolic forms
    (paper Section 6.3), and ``_text_of`` in :mod:`boltzmann.query.scan` reads exactly those
    fields to answer a natural-language query. A block whose statement were empty would be
    unreachable by every text query in the system -- present in the composition, provable
    against the root, and invisible. When the content is binary the statement is what the
    block claims *about* it, which is the part consolidated knowledge is made of.

    Distinct from ``evidence``, which is a citation of another block: content lives and dies
    with the block that names it and nothing else may cite it. Material that other blocks will
    cite is canonical, through ``register``, and always was -- see
    :mod:`boltzmann.blocks.content`.
    """

    SCHEMA_VERSION: ClassVar[int] = 2
