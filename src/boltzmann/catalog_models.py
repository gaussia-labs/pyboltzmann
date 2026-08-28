"""Portable catalog declarations and public response models."""

from __future__ import annotations

from typing import Annotated, Literal, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boltzmann.blocks.semantic import Relation, SemanticBlockV3, SemanticKind
from boltzmann.catalog_core import (
    CATALOG_CROSS_SCHEME,
    CATALOG_CYCLE,
    CATALOG_DUPLICATE,
    CATALOG_EXCLUSIVE_CONFLICT,
    CATALOG_SCHEME_CONFLICT,
    CATALOG_UNKNOWN_CLASS,
    CATALOG_UNKNOWN_SCHEME,
    CATALOG_UNKNOWN_SOURCE,
    CatalogProblem,
    class_label_problem,
)
from boltzmann.identity.digest import BlockId


class SchemeInfo(NamedTuple):
    """Stored identity and cardinality rule for a scheme."""

    block_id: BlockId
    exclusive: bool


class ClassInfo(NamedTuple):
    """Stored scheme and display label for a class."""

    scheme: str
    label: str


class CatalogValidationState(Protocol):
    """Read surface declarations need in order to validate themselves."""

    schemes: dict[str, SchemeInfo]
    classes: dict[BlockId, ClassInfo]
    class_by_label: dict[tuple[str, str], BlockId]
    children: dict[BlockId, set[BlockId]]
    placements: dict[tuple[BlockId, BlockId], BlockId]
    source_classes: dict[BlockId, set[BlockId]]

    def reachable(self, start: BlockId, target: BlockId) -> bool: ...


class SchemeDeclaration(BaseModel):
    """Declare one independent classification dimension."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["scheme"] = "scheme"
    scheme: str = Field(min_length=1)
    exclusive: bool = False

    def to_block(self) -> SemanticBlockV3:
        return SemanticBlockV3(kind=SemanticKind.SCHEME, scheme=self.scheme, exclusive=self.exclusive)

    @property
    def block_id(self) -> BlockId:
        return self.to_block().block_id

    def problems(self, state: CatalogValidationState, canonical_ids: set[BlockId]) -> list[CatalogProblem]:
        del canonical_ids
        existing = state.schemes.get(self.scheme)
        if existing is None:
            return []
        if existing.exclusive != self.exclusive:
            return [
                CatalogProblem(
                    CATALOG_SCHEME_CONFLICT,
                    f"scheme {self.scheme!r} is already declared",
                    conflicts_with=(existing.block_id,),
                )
            ]
        return [
            CatalogProblem(
                CATALOG_DUPLICATE, f"scheme {self.scheme!r} is already declared", conflicts_with=(existing.block_id,)
            )
        ]


class ClassDeclaration(BaseModel):
    """Declare one class in a scheme, independently of its parents."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["class"] = "class"
    scheme: str = Field(min_length=1)
    label: str = Field(min_length=1)

    @field_validator("label")
    @classmethod
    def _path_safe_label(cls, label: str) -> str:
        if (problem := class_label_problem(label)) is not None:
            raise ValueError(problem)
        return label

    def to_block(self) -> SemanticBlockV3:
        return SemanticBlockV3(kind=SemanticKind.CLASS, scheme=self.scheme, label=self.label)

    @property
    def block_id(self) -> BlockId:
        return self.to_block().block_id

    def problems(self, state: CatalogValidationState, canonical_ids: set[BlockId]) -> list[CatalogProblem]:
        del canonical_ids
        if self.scheme not in state.schemes:
            return [
                CatalogProblem(
                    CATALOG_UNKNOWN_SCHEME, f"scheme {self.scheme!r} must be declared before its classes", "scheme"
                )
            ]
        existing = state.class_by_label.get((self.scheme, self.label))
        if existing is not None:
            return [
                CatalogProblem(CATALOG_DUPLICATE, "this catalog class is already declared", conflicts_with=(existing,))
            ]
        return []


class HierarchyDeclaration(BaseModel):
    """Place a class below another class in the same scheme."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["hierarchy"] = "hierarchy"
    broader: BlockId
    narrower: BlockId

    def to_block(self) -> SemanticBlockV3:
        return SemanticBlockV3(
            kind=SemanticKind.RELATION,
            relations=[
                Relation(predicate="broader", target=self.broader),
                Relation(predicate="narrower", target=self.narrower),
            ],
        )

    @property
    def block_id(self) -> BlockId:
        return self.to_block().block_id

    def problems(self, state: CatalogValidationState, canonical_ids: set[BlockId]) -> list[CatalogProblem]:
        del canonical_ids
        if self.broader == self.narrower:
            return [CatalogProblem(CATALOG_CYCLE, "a class cannot be broader than itself")]
        parent = state.classes.get(self.broader)
        child = state.classes.get(self.narrower)
        if parent is None or child is None:
            return [CatalogProblem(CATALOG_UNKNOWN_CLASS, "both hierarchy endpoints must be declared classes")]
        if parent.scheme != child.scheme:
            return [CatalogProblem(CATALOG_CROSS_SCHEME, "a hierarchy edge must stay within one scheme")]
        if self.narrower in state.children.get(self.broader, set()):
            return [
                CatalogProblem(CATALOG_DUPLICATE, "this hierarchy edge already exists", conflicts_with=(self.block_id,))
            ]
        if state.reachable(self.narrower, self.broader):
            return [CatalogProblem(CATALOG_CYCLE, "this hierarchy edge would create a cycle")]
        return []


class PlacementDeclaration(BaseModel):
    """Classify one canonical source as a class."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["placement"] = "placement"
    source: BlockId
    class_id: BlockId

    def to_block(self) -> SemanticBlockV3:
        return SemanticBlockV3(
            kind=SemanticKind.RELATION,
            evidence=[self.source],
            relations=[Relation(predicate="classified_as", target=self.class_id)],
        )

    @property
    def block_id(self) -> BlockId:
        return self.to_block().block_id

    def problems(self, state: CatalogValidationState, canonical_ids: set[BlockId]) -> list[CatalogProblem]:
        if self.source not in canonical_ids:
            return [
                CatalogProblem(
                    CATALOG_UNKNOWN_SOURCE, "a placement source must belong to the canonical composition", "source"
                )
            ]
        class_info = state.classes.get(self.class_id)
        if class_info is None:
            return [
                CatalogProblem(CATALOG_UNKNOWN_CLASS, "a placement target must be a declared catalog class", "class_id")
            ]
        existing = state.placements.get((self.source, self.class_id))
        if existing is not None:
            return [
                CatalogProblem(CATALOG_DUPLICATE, "this source placement already exists", conflicts_with=(existing,))
            ]
        scheme = state.schemes[class_info.scheme]
        if not scheme.exclusive:
            return []
        competing = tuple(
            state.placements[(self.source, placed)]
            for placed in state.source_classes.get(self.source, set())
            if placed != self.class_id and state.classes.get(placed, ClassInfo("", "")).scheme == class_info.scheme
        )
        if competing:
            return [
                CatalogProblem(
                    CATALOG_EXCLUSIVE_CONFLICT,
                    f"source already has another class in exclusive scheme {class_info.scheme!r}",
                    conflicts_with=competing,
                )
            ]
        return []


CatalogDeclaration = Annotated[
    SchemeDeclaration | ClassDeclaration | HierarchyDeclaration | PlacementDeclaration, Field(discriminator="kind")
]


class ClassificationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    declarations: list[CatalogDeclaration] = Field(min_length=1)


class CatalogNode(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    class_id: BlockId
    scheme: str
    label: str
    broader: list[BlockId] = Field(default_factory=list)
    narrower: list[BlockId] = Field(default_factory=list)
    direct_sources: list[BlockId] = Field(default_factory=list)
    sources: list[BlockId] = Field(default_factory=list)


class CatalogBrowseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    classes: list[BlockId]
    nodes: list[CatalogNode]
    sources: list[BlockId]


class CatalogDirectory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    path: str
    scheme: str | None = None
    directories: list[str] = Field(default_factory=list)
    sources: list[BlockId] = Field(default_factory=list)
