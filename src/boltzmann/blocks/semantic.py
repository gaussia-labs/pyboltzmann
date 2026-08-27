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
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler, model_validator
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

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


class SemanticBlockV3(SemanticBlockV2):
    """Semantic schema extended with the catalog blocks from paper Section 6.7.

    Older semantic knowledge still selects v1 or v2 through ``Block.build``. Version 3 is used only
    when the payload has one of the catalog shapes that those schemas cannot express: a scheme, a
    class, a placement, or an independent hierarchy relation.

    ``label`` and ``statement`` become conditionally required. They remain mandatory for the five
    original semantic kinds, while catalog structure carries only the fields that determine it. In
    particular a class has no parent field: moving it changes a relation block, not the class identity.
    """

    SCHEMA_VERSION: ClassVar[int] = 3

    label: str | None = Field(default=None, min_length=1)  # type: ignore[assignment]
    statement: str | None = Field(default=None, min_length=1)  # type: ignore[assignment]
    scheme: str | None = Field(default=None, min_length=1)
    exclusive: bool | None = None

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Describe the same conditional shapes the runtime validator enforces."""
        schema = handler(core_schema)
        catalog_fields = [{"required": [name]} for name in ("scheme", "exclusive")]
        claim_fields = [{"required": [name]} for name in ("label", "statement")]
        metadata_fields = [{"required": [name]} for name in ("subject", "aliases", "content")]
        catalog_predicates = ["classified_as", "broader", "narrower"]
        schema["oneOf"] = [
            {
                "properties": {
                    "kind": {"enum": ["concept", "fact", "formula", "constraint"]},
                },
                "required": ["label", "statement"],
                "not": {"anyOf": catalog_fields},
            },
            {
                "properties": {"kind": {"const": "relation"}},
                "required": ["label", "statement"],
                "not": {
                    "anyOf": [
                        *catalog_fields,
                        {
                            "properties": {
                                "relations": {
                                    "contains": {
                                        "properties": {"predicate": {"enum": catalog_predicates}},
                                        "required": ["predicate"],
                                    }
                                }
                            },
                            "required": ["relations"],
                        },
                    ]
                },
            },
            {
                "properties": {"kind": {"const": "scheme"}},
                "required": ["scheme", "exclusive"],
                "not": {
                    "anyOf": [*claim_fields, *metadata_fields, {"required": ["evidence"]}, {"required": ["relations"]}]
                },
            },
            {
                "properties": {"kind": {"const": "class"}},
                "required": ["scheme", "label"],
                "not": {
                    "anyOf": [
                        {"required": ["statement"]},
                        {"required": ["exclusive"]},
                        *metadata_fields,
                        {"required": ["evidence"]},
                        {"required": ["relations"]},
                    ]
                },
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
                "not": {"anyOf": [*claim_fields, *catalog_fields, *metadata_fields]},
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
                "not": {
                    "anyOf": [
                        *claim_fields,
                        *catalog_fields,
                        *metadata_fields,
                        {"required": ["evidence"]},
                    ]
                },
            },
        ]
        return schema

    @model_validator(mode="after")
    def _catalog_shape(self) -> Self:
        legacy = {
            SemanticKind.CONCEPT,
            SemanticKind.FACT,
            SemanticKind.FORMULA,
            SemanticKind.CONSTRAINT,
        }
        if self.kind in legacy:
            self._require("label", self.label)
            self._require("statement", self.statement)
            self._forbid_catalog_fields()
            return self

        if self.kind is SemanticKind.SCHEME:
            self._require("scheme", self.scheme)
            if self.exclusive is None:
                raise ValueError("a catalog scheme must declare whether it is exclusive")
            self._forbid("label", self.label)
            self._forbid("statement", self.statement)
            self._forbid("evidence", self.evidence)
            self._forbid("relations", self.relations)
            self._forbid("aliases", self.aliases)
            self._forbid("subject", self.subject)
            self._forbid("content", self.content)
            return self

        if self.kind is SemanticKind.CLASS:
            self._require("scheme", self.scheme)
            self._require("label", self.label)
            self._forbid("exclusive", self.exclusive)
            self._forbid("statement", self.statement)
            self._forbid("evidence", self.evidence)
            self._forbid("relations", self.relations)
            self._forbid("aliases", self.aliases)
            self._forbid("subject", self.subject)
            self._forbid("content", self.content)
            return self

        if self.kind is SemanticKind.RELATION:
            self._forbid_catalog_fields()
            predicates = [relation.predicate for relation in self.relations or []]
            if predicates == ["classified_as"]:
                if not self.evidence or len(self.evidence) != 1:
                    raise ValueError("a catalog placement must cite exactly one canonical block as evidence")
                self._forbid("label", self.label)
                self._forbid("statement", self.statement)
                self._forbid("aliases", self.aliases)
                self._forbid("subject", self.subject)
                self._forbid("content", self.content)
                return self
            if predicates == ["broader", "narrower"]:
                self._forbid("evidence", self.evidence)
                self._forbid("label", self.label)
                self._forbid("statement", self.statement)
                self._forbid("aliases", self.aliases)
                self._forbid("subject", self.subject)
                self._forbid("content", self.content)
                targets = {relation.target for relation in self.relations or []}
                if len(targets) != 2:
                    raise ValueError("a catalog hierarchy must name two distinct class endpoints")
                return self

            if set(predicates) & {"classified_as", "broader", "narrower"}:
                raise ValueError("catalog relation predicates must use a complete catalog relation shape")

            # The original, general relation block remains available under v3 for completeness, although
            # oldest-that-fits normally writes it as v1 or v2.
            self._require("label", self.label)
            self._require("statement", self.statement)
            return self

        raise ValueError(f"unsupported semantic kind {self.kind.value!r}")

    def _forbid_catalog_fields(self) -> None:
        self._forbid("scheme", self.scheme)
        self._forbid("exclusive", self.exclusive)

    @staticmethod
    def _require(name: str, value: Any) -> None:
        if value is None or value == "":
            raise ValueError(f"{name} is required for this semantic kind")

    @staticmethod
    def _forbid(name: str, value: Any) -> None:
        if value is not None:
            raise ValueError(f"{name} is not allowed for this semantic kind")
