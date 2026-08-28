"""In-memory hierarchical catalog view (paper Section 6.7).

Only the semantic declarations are portable. This module deterministically rebuilds navigation and
faceted paths from them; it stores no additional catalog state.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING
from urllib.parse import unquote

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.catalog_core import CatalogRelationKind, catalog_relation_kind
from boltzmann.catalog_models import (
    CatalogBrowseResult,
    CatalogDeclaration,
    CatalogDirectory,
    CatalogNode,
    ClassDeclaration,
    ClassificationRequest,
    HierarchyDeclaration,
    PlacementDeclaration,
    SchemeDeclaration,
)
from boltzmann.catalog_state import load_catalog_state
from boltzmann.exceptions import CatalogError
from boltzmann.identity.digest import BlockId
from boltzmann.module.module import Module

if TYPE_CHECKING:
    from boltzmann.brain import Brain
    from boltzmann.catalog_validation import ClassificationResult


class Catalog:
    """Read-only catalog reconstructed from installed modules."""

    def __init__(self, modules: dict[MemoryType, Module]) -> None:
        self._state = load_catalog_state(modules)

    @property
    def schemes(self) -> tuple[str, ...]:
        return tuple(sorted(self._state.schemes))

    def class_id(self, scheme: str, label: str) -> BlockId:
        try:
            return self._state.class_by_label[(scheme, label)]
        except KeyError as error:
            raise CatalogError(f"catalog class {label!r} is not declared in scheme {scheme!r}") from error

    def classes_in(self, scheme: str) -> tuple[tuple[str, BlockId], ...]:
        """Return every declared label and class identity in one scheme."""
        return self._state.classes_in(scheme)

    def sources_for(self, class_id: BlockId, include_descendants: bool = True) -> set[BlockId]:
        """Return direct sources and, by default, sources in descendant classes."""
        if class_id not in self._state.classes:
            raise CatalogError(f"catalog class {class_id.short} is not declared")
        if include_descendants:
            return set(self._state.sources_for(class_id))
        return {source for source, placed in self._state.source_classes.items() if class_id in placed}

    def browse(self, classes: BlockId | Sequence[BlockId]) -> CatalogBrowseResult:
        """Browse one class or intersect multiple facets; descendants are included for each facet."""
        selected = [classes] if isinstance(classes, BlockId) else list(classes)
        sets = [self.sources_for(class_id) for class_id in selected]
        sources = set.intersection(*sets) if sets else set(self._state.source_classes)
        return CatalogBrowseResult(
            classes=selected, nodes=[self._node(item) for item in selected], sources=_sorted_ids(sources)
        )

    def _node(self, class_id: BlockId) -> CatalogNode:
        try:
            info = self._state.classes[class_id]
        except KeyError as error:
            raise CatalogError(f"catalog class {class_id.short} is not declared") from error
        return CatalogNode(
            class_id=class_id,
            scheme=info.scheme,
            label=info.label,
            broader=_sorted_ids(self._state.parents.get(class_id, set())),
            narrower=_sorted_ids(self._state.children.get(class_id, set())),
            direct_sources=_sorted_ids(
                source for source, placed in self._state.source_classes.items() if class_id in placed
            ),
            sources=_sorted_ids(self._state.sources_for(class_id)),
        )


class CatalogPathView:
    """Virtual slash-separated navigation over a caller-chosen ordering of independent schemes."""

    def __init__(self, brain: Brain, schemes: Sequence[str]) -> None:
        self._brain = brain
        self.schemes = tuple(schemes)
        if not self.schemes:
            raise CatalogError("a catalog path view needs at least one scheme")
        if len(set(self.schemes)) != len(self.schemes):
            raise CatalogError("a catalog path view cannot repeat a scheme")
        missing = [scheme for scheme in self.schemes if scheme not in Catalog(brain.modules()).schemes]
        if missing:
            raise CatalogError(f"catalog schemes are not declared: {', '.join(repr(item) for item in missing)}")

    def browse(self, path: str = "") -> CatalogBrowseResult:
        """Resolve a path prefix and return the AND intersection of its selected facets."""
        catalog = Catalog(self._brain.modules())
        return catalog.browse(self._resolve(catalog, path))

    def iterdir(self, path: str = "") -> CatalogDirectory:
        """List viable next labels, or canonical sources after the final segment."""
        catalog = Catalog(self._brain.modules())
        parts = _path_parts(path, len(self.schemes))
        selected = self._resolve_parts(catalog, parts)
        normalized = "/".join(parts)
        if len(parts) == len(self.schemes):
            return CatalogDirectory(path=normalized, sources=catalog.browse(selected).sources)
        scheme = self.schemes[len(parts)]
        prefix_sources = set(catalog.browse(selected).sources)
        directories = [
            label for label, class_id in catalog.classes_in(scheme) if prefix_sources & catalog.sources_for(class_id)
        ]
        return CatalogDirectory(path=normalized, scheme=scheme, directories=directories)

    def classify(self, source: BlockId, path: str) -> ClassificationResult:
        """Place one canonical source in every class of a complete path, in a single commit."""
        catalog = Catalog(self._brain.modules())
        classes = self._resolve_parts(catalog, _path_parts(path, len(self.schemes), require_full=True))
        return self._brain.classify([PlacementDeclaration(source=source, class_id=class_id) for class_id in classes])

    def _resolve(self, catalog: Catalog, path: str) -> list[BlockId]:
        return self._resolve_parts(catalog, _path_parts(path, len(self.schemes)))

    def _resolve_parts(self, catalog: Catalog, parts: Sequence[str]) -> list[BlockId]:
        return [catalog.class_id(self.schemes[index], label) for index, label in enumerate(parts)]


def _path_parts(path: str, maximum: int, require_full: bool = False) -> list[str]:
    stripped = path.strip("/")
    raw = [] if not stripped else stripped.split("/")
    if any(not part for part in raw):
        raise CatalogError("catalog paths cannot contain an empty segment")
    try:
        parts = [unquote(part, errors="strict") for part in raw]
    except UnicodeDecodeError as error:
        raise CatalogError("catalog path contains an invalid percent-encoded segment") from error
    if any(part in {".", ".."} or not part or "/" in part for part in parts):
        raise CatalogError("catalog paths cannot contain '/', '.', '..', or an empty decoded segment")
    if len(parts) > maximum:
        raise CatalogError(f"catalog path has {len(parts)} segments; this view accepts at most {maximum}")
    if require_full and len(parts) != maximum:
        raise CatalogError(f"classification requires all {maximum} path segments")
    return parts


def evidence_sources(block_id: BlockId, block: Block) -> set[BlockId]:
    """Return the canonical identities represented by a query candidate."""
    if block.MEMORY_TYPE is MemoryType.CANONICAL:
        return {block_id}
    return set(getattr(block, "evidence", None) or [])


def declaration_from_block(block: Block) -> CatalogDeclaration | None:
    """Recover an SDK declaration from any exact catalog-shaped semantic block version."""
    kind = getattr(block, "kind", None)
    if kind == "scheme" or getattr(kind, "value", None) == "scheme":
        scheme, exclusive = getattr(block, "scheme", None), getattr(block, "exclusive", None)
        return (
            SchemeDeclaration(scheme=scheme, exclusive=exclusive)
            if isinstance(scheme, str) and isinstance(exclusive, bool)
            else None
        )
    if kind == "class" or getattr(kind, "value", None) == "class":
        scheme, label = getattr(block, "scheme", None), getattr(block, "label", None)
        return (
            ClassDeclaration(scheme=scheme, label=label) if isinstance(scheme, str) and isinstance(label, str) else None
        )
    relations = getattr(block, "relations", None)
    relation_kind = catalog_relation_kind(relations)
    if relation_kind is CatalogRelationKind.HIERARCHY and relations:
        return HierarchyDeclaration(broader=relations[0].target, narrower=relations[1].target)
    if relation_kind is CatalogRelationKind.PLACEMENT and relations:
        evidence = getattr(block, "evidence", None)
        if evidence and len(evidence) == 1:
            return PlacementDeclaration(source=evidence[0], class_id=relations[0].target)
    return None


def _sorted_ids(values: Iterable[BlockId]) -> list[BlockId]:
    return sorted(values, key=str)


__all__ = [
    "Catalog",
    "CatalogBrowseResult",
    "CatalogDeclaration",
    "CatalogDirectory",
    "CatalogNode",
    "CatalogPathView",
    "ClassDeclaration",
    "ClassificationRequest",
    "HierarchyDeclaration",
    "PlacementDeclaration",
    "SchemeDeclaration",
    "declaration_from_block",
    "evidence_sources",
]
