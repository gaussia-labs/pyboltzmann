"""Dependency-free catalog vocabulary shared across the SDK (paper Section 6.7)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from boltzmann.identity.digest import BlockId

CATALOG_CYCLE = "catalog-cycle"
CATALOG_CROSS_SCHEME = "catalog-cross-scheme"
CATALOG_DECLARATION_NOT_ALLOWED = "catalog-declaration-not-allowed"
CATALOG_DUPLICATE = "catalog-duplicate"
CATALOG_EXCLUSIVE_CONFLICT = "catalog-exclusive-conflict"
CATALOG_INVALID_LABEL = "catalog-invalid-label"
CATALOG_SCHEME_CONFLICT = "catalog-scheme-conflict"
CATALOG_UNKNOWN_CLASS = "catalog-unknown-class"
CATALOG_UNKNOWN_SCHEME = "catalog-unknown-scheme"
CATALOG_UNKNOWN_SOURCE = "catalog-unknown-source"

CATALOG_CONTRADICTION_CODES = frozenset({CATALOG_EXCLUSIVE_CONFLICT, CATALOG_SCHEME_CONFLICT})
CATALOG_PREDICATES = frozenset({"classified_as", "broader", "narrower"})


class RelationLike(Protocol):
    """The relation fields catalog classification needs, independent of its pydantic owner."""

    predicate: str
    target: BlockId


class CatalogRelationKind(StrEnum):
    """The two relation shapes reserved by the catalog."""

    PLACEMENT = "placement"
    HIERARCHY = "hierarchy"


@dataclass(frozen=True)
class CatalogProblem:
    """A declaration problem before it is translated to the ingestion verdict vocabulary."""

    code: str
    detail: str
    field: str | None = None
    conflicts_with: tuple[BlockId, ...] = ()

    @property
    def contradicted(self) -> bool:
        """Whether the problem describes competing valid state rather than malformed input."""
        return self.code in CATALOG_CONTRADICTION_CODES


def catalog_relation_kind(relations: Sequence[RelationLike] | None) -> CatalogRelationKind | None:
    """Classify one exact catalog relation shape, or return ``None`` for an ordinary relation."""
    predicates = tuple(relation.predicate for relation in relations or ())
    if predicates == ("classified_as",):
        return CatalogRelationKind.PLACEMENT
    if predicates == ("broader", "narrower"):
        return CatalogRelationKind.HIERARCHY
    return None


def uses_catalog_predicate(relations: Sequence[RelationLike] | None) -> bool:
    """Whether any relation uses a predicate reserved for a complete catalog shape."""
    return any(relation.predicate in CATALOG_PREDICATES for relation in relations or ())


def class_label_problem(label: str) -> str | None:
    """Explain why a class label cannot be represented as one exact path segment."""
    if label in {".", ".."}:
        return "catalog class labels cannot be '.' or '..'"
    if "/" in label:
        return "catalog class labels cannot contain '/'"
    return None
