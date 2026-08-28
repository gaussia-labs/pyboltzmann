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
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler, field_validator, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from boltzmann.blocks.base import Block
from boltzmann.blocks.content import NamesContent
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.catalog_core import (
    CatalogRelationKind,
    catalog_relation_kind,
    class_label_problem,
    uses_catalog_predicate,
)
from boltzmann.identity.digest import BlockId


class SemanticKind(StrEnum):
    """What kind of consolidated knowledge a semantic block states."""

    CONCEPT = "concept"
    FACT = "fact"
    FORMULA = "formula"
    RELATION = "relation"
    CONSTRAINT = "constraint"
    SCHEME = "scheme"
    CLASS = "class"


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

    @model_validator(mode="after")
    def _catalog_kinds_need_v3(self) -> Self:
        if self.SCHEMA_VERSION < 3 and self.kind in {SemanticKind.SCHEME, SemanticKind.CLASS}:
            raise ValueError("catalog semantic kinds require schema version 3")
        return self

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Keep catalog-only enum values out of ordinary semantic claim schemas."""
        schema = handler(core_schema)
        kind = schema.get("properties", {}).get("kind")
        if isinstance(kind, dict):
            kind.pop("$ref", None)
            kind["enum"] = [
                item.value for item in SemanticKind if item not in {SemanticKind.SCHEME, SemanticKind.CLASS}
            ]
        return schema


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


class SemanticBlockV3(Block):
    """Portable catalog structure stored in semantic memory (paper Section 6.7).

    This is deliberately a sibling of :class:`SemanticBlock`, not a subtype. Catalog structure has
    no claim ``statement`` and only classes have a ``label``; ordinary semantic claims therefore keep
    their non-optional interface while the registry still decodes semantic schema version 3.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.SEMANTIC
    SCHEMA_VERSION: ClassVar[int] = 3

    kind: SemanticKind
    scheme: str | None = Field(default=None, min_length=1)
    label: str | None = Field(default=None, min_length=1)
    exclusive: bool | None = None
    evidence: list[BlockId] | None = None
    relations: list[Relation] | None = None

    @field_validator("label")
    @classmethod
    def _path_safe_label(cls, label: str | None) -> str | None:
        if label is not None and (problem := class_label_problem(label)) is not None:
            raise ValueError(problem)
        return label

    @model_validator(mode="after")
    def _catalog_shape(self) -> Self:
        if self.kind is SemanticKind.SCHEME:
            if self.scheme is None or self.exclusive is None:
                raise ValueError("a catalog scheme requires scheme and exclusive")
            if self.label is not None or self.evidence is not None or self.relations is not None:
                raise ValueError("a catalog scheme carries only scheme and exclusive")
            return self

        if self.kind is SemanticKind.CLASS:
            if self.scheme is None or self.label is None:
                raise ValueError("a catalog class requires scheme and label")
            if self.exclusive is not None or self.evidence is not None or self.relations is not None:
                raise ValueError("a catalog class carries only scheme and label")
            return self

        if self.kind is not SemanticKind.RELATION:
            raise ValueError("semantic schema version 3 is reserved for catalog structure")
        if self.scheme is not None or self.label is not None or self.exclusive is not None:
            raise ValueError("a catalog relation carries no scheme, label, or exclusive field")

        relation_kind = catalog_relation_kind(self.relations)
        if relation_kind is CatalogRelationKind.PLACEMENT:
            if self.evidence is None or len(self.evidence) != 1:
                raise ValueError("a catalog placement must cite exactly one canonical block as evidence")
            return self
        if relation_kind is CatalogRelationKind.HIERARCHY:
            if self.evidence is not None:
                raise ValueError("a catalog hierarchy carries no canonical evidence")
            return self
        if uses_catalog_predicate(self.relations):
            raise ValueError("catalog relation predicates must use a complete catalog relation shape")
        raise ValueError("semantic schema version 3 relations must be catalog placements or hierarchy edges")

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Describe the same four mutually exclusive catalog shapes enforced at runtime."""
        schema = handler(core_schema)
        schema["oneOf"] = [
            {
                "properties": {"kind": {"const": "scheme"}},
                "required": ["scheme", "exclusive"],
                "not": {"anyOf": [{"required": [name]} for name in ("label", "evidence", "relations")]},
            },
            {
                "properties": {"kind": {"const": "class"}},
                "required": ["scheme", "label"],
                "not": {"anyOf": [{"required": [name]} for name in ("exclusive", "evidence", "relations")]},
            },
            {
                "properties": {
                    "kind": {"const": "relation"},
                    "relations": {
                        "minItems": 1,
                        "maxItems": 1,
                        "prefixItems": [
                            {
                                "properties": {"predicate": {"const": "classified_as"}},
                                "required": ["predicate", "target"],
                            }
                        ],
                    },
                },
                "required": ["relations"],
                "not": {"anyOf": [{"required": [name]} for name in ("scheme", "label", "exclusive")]},
            },
            {
                "properties": {
                    "kind": {"const": "relation"},
                    "relations": {
                        "minItems": 2,
                        "maxItems": 2,
                        "prefixItems": [
                            {
                                "properties": {"predicate": {"const": "broader"}},
                                "required": ["predicate", "target"],
                            },
                            {
                                "properties": {"predicate": {"const": "narrower"}},
                                "required": ["predicate", "target"],
                            },
                        ],
                    },
                },
                "required": ["relations"],
                "not": {"anyOf": [{"required": [name]} for name in ("scheme", "label", "exclusive", "evidence")]},
            },
        ]
        return schema
