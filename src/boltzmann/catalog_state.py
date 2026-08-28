"""Catalog state reconstructed from accessible semantic blocks."""

from __future__ import annotations

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import SemanticKind
from boltzmann.catalog_core import CatalogRelationKind, catalog_relation_kind
from boltzmann.catalog_models import ClassInfo, SchemeInfo
from boltzmann.identity.digest import BlockId
from boltzmann.module.ledger import Ledger
from boltzmann.module.module import Module


class CatalogState:
    """Mutable load/validation state with named records and cached source closures."""

    def __init__(self) -> None:
        self.schemes: dict[str, SchemeInfo] = {}
        self.classes: dict[BlockId, ClassInfo] = {}
        self.class_by_label: dict[tuple[str, str], BlockId] = {}
        self.parents: dict[BlockId, set[BlockId]] = {}
        self.children: dict[BlockId, set[BlockId]] = {}
        self.placements: dict[tuple[BlockId, BlockId], BlockId] = {}
        self.source_classes: dict[BlockId, set[BlockId]] = {}
        self._class_sources: dict[BlockId, frozenset[BlockId]] = {}

    def add_block(self, block: Block) -> None:
        kind = getattr(block, "kind", None)
        if kind is SemanticKind.SCHEME:
            scheme = getattr(block, "scheme", None)
            exclusive = getattr(block, "exclusive", None)
            if isinstance(scheme, str) and isinstance(exclusive, bool):
                self.schemes[scheme] = SchemeInfo(block.block_id, exclusive)
            return
        if kind is SemanticKind.CLASS:
            scheme = getattr(block, "scheme", None)
            label = getattr(block, "label", None)
            if isinstance(scheme, str) and isinstance(label, str):
                self.classes[block.block_id] = ClassInfo(scheme, label)
                self.class_by_label[(scheme, label)] = block.block_id
            return
        relations = getattr(block, "relations", None)
        relation_kind = catalog_relation_kind(relations)
        if relation_kind is CatalogRelationKind.HIERARCHY:
            assert relations is not None
            parent, child = relations[0].target, relations[1].target
            self.parents.setdefault(child, set()).add(parent)
            self.children.setdefault(parent, set()).add(child)
        elif relation_kind is CatalogRelationKind.PLACEMENT:
            evidence = getattr(block, "evidence", None)
            if evidence and len(evidence) == 1 and relations:
                source, class_id = evidence[0], relations[0].target
                self.placements[(source, class_id)] = block.block_id
                self.source_classes.setdefault(source, set()).add(class_id)
        self._class_sources.clear()

    def prune_dangling(self) -> None:
        """Ignore edges whose endpoints were unavailable, redacted, or superseded."""
        valid_classes = set(self.classes)
        self.parents = {
            child: parents & valid_classes for child, parents in self.parents.items() if child in valid_classes
        }
        self.children = {
            parent: children & valid_classes for parent, children in self.children.items() if parent in valid_classes
        }
        self.placements = {
            (source, class_id): block_id
            for (source, class_id), block_id in self.placements.items()
            if class_id in valid_classes
        }
        self.source_classes = {}
        for source, class_id in self.placements:
            self.source_classes.setdefault(source, set()).add(class_id)
        self._class_sources.clear()

    def reachable(self, start: BlockId, target: BlockId) -> bool:
        frontier, seen = [start], set()
        while frontier:
            current = frontier.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                frontier.extend(self.children.get(current, set()))
        return False

    def classes_in(self, scheme: str) -> tuple[tuple[str, BlockId], ...]:
        """Return path labels and identities for one scheme without exposing private maps."""
        return tuple(
            sorted(
                (label, class_id) for (candidate, label), class_id in self.class_by_label.items() if candidate == scheme
            )
        )

    def sources_for(self, class_id: BlockId) -> frozenset[BlockId]:
        cached = self._class_sources.get(class_id)
        if cached is not None:
            return cached
        classes, frontier = {class_id}, [class_id]
        while frontier:
            current = frontier.pop()
            for child in self.children.get(current, set()):
                if child not in classes:
                    classes.add(child)
                    frontier.append(child)
        sources = frozenset(source for source, placed in self.source_classes.items() if classes & placed)
        self._class_sources[class_id] = sources
        return sources


def load_catalog_state(
    modules: dict[MemoryType, Module], ignore_blocks: set[BlockId] | frozenset[BlockId] = frozenset()
) -> CatalogState:
    """Load only resolvable, accessible catalog structure; damaged entries do not brick the view."""
    state = CatalogState()
    semantic = modules.get(MemoryType.SEMANTIC)
    if semantic is None:
        return state
    ledger = Ledger.of(modules)
    for block_id in semantic.block_ids:
        if (
            block_id in ignore_blocks
            or not ledger.is_accessible(block_id)
            or not semantic.store.is_resolvable(block_id)
        ):
            continue
        state.add_block(semantic.get(block_id))
    state.prune_dangling()
    return state
