"""Hierarchical catalog rebuilt from portable semantic blocks.

The catalog is a derived view, not a sixth memory module.  Schemes, classes,
hierarchy edges, and placements are semantic blocks; this module validates those
blocks and reconstructs the convenient navigation API in memory.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import Relation, SemanticBlockV3, SemanticKind
from boltzmann.exceptions import CatalogError
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.validation import ValidationIssue, ValidationStatus
from boltzmann.module.module import Module

if TYPE_CHECKING:
    from boltzmann.brain import Brain


class SchemeDeclaration(BaseModel):
    """Declare one independent classification dimension."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["scheme"] = "scheme"
    scheme: str = Field(min_length=1)
    exclusive: bool = False

    def to_block(self) -> SemanticBlockV3:
        """Return the portable semantic block for this declaration."""
        return SemanticBlockV3(kind=SemanticKind.SCHEME, scheme=self.scheme, exclusive=self.exclusive)

    @property
    def block_id(self) -> BlockId:
        """Identity the declaration will have if accepted."""
        return self.to_block().block_id


class ClassDeclaration(BaseModel):
    """Declare one class in a scheme, independently of its parents."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["class"] = "class"
    scheme: str = Field(min_length=1)
    label: str = Field(min_length=1)

    def to_block(self) -> SemanticBlockV3:
        """Return the portable semantic block for this declaration."""
        return SemanticBlockV3(kind=SemanticKind.CLASS, scheme=self.scheme, label=self.label)

    @property
    def block_id(self) -> BlockId:
        """Identity the declaration will have if accepted."""
        return self.to_block().block_id


class HierarchyDeclaration(BaseModel):
    """Place a class below another class in the same scheme."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["hierarchy"] = "hierarchy"
    broader: BlockId
    narrower: BlockId

    def to_block(self) -> SemanticBlockV3:
        """Return the portable relation block for this edge."""
        return SemanticBlockV3(
            kind=SemanticKind.RELATION,
            relations=[
                Relation(predicate="broader", target=self.broader),
                Relation(predicate="narrower", target=self.narrower),
            ],
        )

    @property
    def block_id(self) -> BlockId:
        """Identity the declaration will have if accepted."""
        return self.to_block().block_id


class PlacementDeclaration(BaseModel):
    """Classify one canonical source as a class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["placement"] = "placement"
    source: BlockId
    class_id: BlockId

    def to_block(self) -> SemanticBlockV3:
        """Return the portable relation block for this placement."""
        return SemanticBlockV3(
            kind=SemanticKind.RELATION,
            evidence=[self.source],
            relations=[Relation(predicate="classified_as", target=self.class_id)],
        )

    @property
    def block_id(self) -> BlockId:
        """Identity the declaration will have if accepted."""
        return self.to_block().block_id


CatalogDeclaration = Annotated[
    SchemeDeclaration | ClassDeclaration | HierarchyDeclaration | PlacementDeclaration,
    Field(discriminator="kind"),
]


class ClassificationRequest(BaseModel):
    """A batch of catalog declarations validated in order and committed atomically."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    declarations: list[CatalogDeclaration] = Field(min_length=1)


class CatalogVerdict(BaseModel):
    """Validation outcome for one declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    kind: str
    status: ValidationStatus
    block_id: BlockId
    issues: list[ValidationIssue] = Field(default_factory=list)
    conflicts_with: list[BlockId] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    """Catalog verdicts and the single commit they produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[CatalogVerdict]
    commit: CommitResult

    @property
    def is_clean(self) -> bool:
        """Whether every declaration was accepted."""
        return all(verdict.status is ValidationStatus.VALIDATED for verdict in self.verdicts)


class CatalogNode(BaseModel):
    """One class and the hierarchy reconstructed around it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_id: BlockId
    scheme: str
    label: str
    broader: list[BlockId] = Field(default_factory=list)
    narrower: list[BlockId] = Field(default_factory=list)
    direct_sources: list[BlockId] = Field(default_factory=list)
    sources: list[BlockId] = Field(default_factory=list)


class CatalogBrowseResult(BaseModel):
    """The selected classes and canonical sources below them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classes: list[BlockId]
    nodes: list[CatalogNode]
    sources: list[BlockId]


class CatalogDirectory(BaseModel):
    """One virtual directory in a faceted catalog path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    scheme: str | None = None
    directories: list[str] = Field(default_factory=list)
    sources: list[BlockId] = Field(default_factory=list)


class _CatalogState:
    """Mutable state used while loading and validating a catalog."""

    def __init__(self) -> None:
        self.schemes: dict[str, tuple[BlockId, bool]] = {}
        self.classes: dict[BlockId, tuple[str, str]] = {}
        self.class_by_label: dict[tuple[str, str], BlockId] = {}
        self.parents: dict[BlockId, set[BlockId]] = {}
        self.children: dict[BlockId, set[BlockId]] = {}
        self.placements: dict[tuple[BlockId, BlockId], BlockId] = {}
        self.source_classes: dict[BlockId, set[BlockId]] = {}

    def add_block(self, block: SemanticBlockV3) -> None:
        if block.kind is SemanticKind.SCHEME:
            assert block.scheme is not None
            assert block.exclusive is not None
            self.schemes[block.scheme] = (block.block_id, block.exclusive)
            return
        if block.kind is SemanticKind.CLASS:
            assert block.scheme is not None
            assert block.label is not None
            self.classes[block.block_id] = (block.scheme, block.label)
            self.class_by_label[(block.scheme, block.label)] = block.block_id
            return
        if block.kind is not SemanticKind.RELATION or not block.relations:
            return
        predicates = {relation.predicate: relation.target for relation in block.relations}
        if set(predicates) == {"broader", "narrower"}:
            parent = predicates["broader"]
            child = predicates["narrower"]
            self.parents.setdefault(child, set()).add(parent)
            self.children.setdefault(parent, set()).add(child)
        elif set(predicates) == {"classified_as"} and block.evidence:
            source = block.evidence[0]
            class_id = predicates["classified_as"]
            self.placements[(source, class_id)] = block.block_id
            self.source_classes.setdefault(source, set()).add(class_id)


class Catalog:
    """Read-only catalog reconstructed from installed modules."""

    def __init__(self, modules: dict[MemoryType, Module]) -> None:
        self._state = _load_state(modules)

    @property
    def schemes(self) -> tuple[str, ...]:
        """Declared scheme names in deterministic order."""
        return tuple(sorted(self._state.schemes))

    def class_id(self, scheme: str, label: str) -> BlockId:
        """Resolve an exact, case-sensitive class label within a scheme."""
        try:
            return self._state.class_by_label[(scheme, label)]
        except KeyError as error:
            raise CatalogError(f"catalog class {label!r} is not declared in scheme {scheme!r}") from error

    def sources_for(self, class_id: BlockId, include_descendants: bool = True) -> set[BlockId]:
        """Canonical sources placed directly or below a class."""
        if class_id not in self._state.classes:
            raise CatalogError(f"catalog class {class_id.short} is not declared")
        classes = {class_id}
        if include_descendants:
            frontier = [class_id]
            while frontier:
                current = frontier.pop()
                for child in self._state.children.get(current, set()):
                    if child not in classes:
                        classes.add(child)
                        frontier.append(child)
        return {source for source, placed in self._state.source_classes.items() if classes & placed}

    def browse(self, classes: BlockId | Sequence[BlockId]) -> CatalogBrowseResult:
        """Browse one class, or the intersection of several faceted classes."""
        selected = [classes] if isinstance(classes, BlockId) else list(classes)
        if not selected:
            sources = set(self._state.source_classes)
        else:
            sets = [self.sources_for(class_id) for class_id in selected]
            sources = set.intersection(*sets) if sets else set()
        nodes = [self._node(class_id) for class_id in selected]
        return CatalogBrowseResult(classes=selected, nodes=nodes, sources=_sorted_ids(sources))

    def _node(self, class_id: BlockId) -> CatalogNode:
        try:
            scheme, label = self._state.classes[class_id]
        except KeyError as error:
            raise CatalogError(f"catalog class {class_id.short} is not declared") from error
        direct = _sorted_ids(
            source for (source, placed), _block_id in self._state.placements.items() if placed == class_id
        )
        return CatalogNode(
            class_id=class_id,
            scheme=scheme,
            label=label,
            broader=_sorted_ids(self._state.parents.get(class_id, set())),
            narrower=_sorted_ids(self._state.children.get(class_id, set())),
            direct_sources=direct,
            sources=_sorted_ids(self.sources_for(class_id)),
        )


class CatalogPathView:
    """A virtual slash-separated view over an ordered set of schemes."""

    def __init__(self, brain: Brain, schemes: Sequence[str]) -> None:
        self._brain = brain
        self.schemes = tuple(schemes)
        if not self.schemes:
            raise CatalogError("a catalog path view needs at least one scheme")
        if len(set(self.schemes)) != len(self.schemes):
            raise CatalogError("a catalog path view cannot repeat a scheme")
        catalog = Catalog(brain.modules())
        missing = [scheme for scheme in self.schemes if scheme not in catalog.schemes]
        if missing:
            raise CatalogError(f"catalog schemes are not declared: {', '.join(repr(item) for item in missing)}")

    def browse(self, path: str = "") -> CatalogBrowseResult:
        """Return the source intersection selected by a path prefix."""
        catalog = Catalog(self._brain.modules())
        return catalog.browse(self._resolve(catalog, path))

    def iterdir(self, path: str = "") -> CatalogDirectory:
        """List the next facet values, or sources when the path is complete."""
        catalog = Catalog(self._brain.modules())
        parts = _path_parts(path, len(self.schemes))
        selected = self._resolve_parts(catalog, parts)
        normalized = "/".join(parts)
        if len(parts) == len(self.schemes):
            return CatalogDirectory(path=normalized, sources=catalog.browse(selected).sources)

        scheme = self.schemes[len(parts)]
        prefix_sources = set(catalog.browse(selected).sources)
        directories = []
        for (candidate_scheme, label), class_id in catalog._state.class_by_label.items():
            if candidate_scheme != scheme:
                continue
            if prefix_sources & catalog.sources_for(class_id):
                directories.append(label)
        return CatalogDirectory(path=normalized, scheme=scheme, directories=sorted(directories))

    def classify(self, source: BlockId, path: str) -> ClassificationResult:
        """Place a canonical source in every class of one complete faceted path."""
        catalog = Catalog(self._brain.modules())
        parts = _path_parts(path, len(self.schemes), require_full=True)
        classes = self._resolve_parts(catalog, parts)
        return self._brain.classify(
            ClassificationRequest(
                declarations=[PlacementDeclaration(source=source, class_id=class_id) for class_id in classes]
            )
        )

    def _resolve(self, catalog: Catalog, path: str) -> list[BlockId]:
        return self._resolve_parts(catalog, _path_parts(path, len(self.schemes)))

    def _resolve_parts(self, catalog: Catalog, parts: Sequence[str]) -> list[BlockId]:
        return [catalog.class_id(self.schemes[index], label) for index, label in enumerate(parts)]


def _path_parts(path: str, maximum: int, require_full: bool = False) -> list[str]:
    stripped = path.strip("/")
    if not stripped:
        parts: list[str] = []
    else:
        raw = stripped.split("/")
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


def _load_state(
    modules: dict[MemoryType, Module],
    ignore_blocks: set[BlockId] | frozenset[BlockId] = frozenset(),
) -> _CatalogState:
    state = _CatalogState()
    semantic = modules.get(MemoryType.SEMANTIC)
    if semantic is None:
        return state
    for block_id in semantic.block_ids:
        if block_id in ignore_blocks:
            continue
        if not semantic.store.is_resolvable(block_id):
            raise CatalogError(f"cannot rebuild catalog: semantic block {block_id.short} is not resolvable")
        block = semantic.get(block_id)
        if isinstance(block, SemanticBlockV3):
            state.add_block(block)
    return state


def validate_declarations(
    request: ClassificationRequest,
    modules: dict[MemoryType, Module],
    ignore_blocks: set[BlockId] | frozenset[BlockId] = frozenset(),
) -> tuple[list[CatalogVerdict], list[SemanticBlockV3], list[PlacementDeclaration]]:
    """Validate a declaration batch sequentially against the installed catalog."""
    state = _load_state(modules, ignore_blocks)
    canonical = modules.get(MemoryType.CANONICAL)
    verdicts: list[CatalogVerdict] = []
    accepted: list[SemanticBlockV3] = []
    placements: list[PlacementDeclaration] = []

    for index, declaration in enumerate(request.declarations):
        block = declaration.to_block()
        issues: list[ValidationIssue] = []
        conflicts: list[BlockId] = []
        status = ValidationStatus.VALIDATED

        if isinstance(declaration, SchemeDeclaration):
            existing = state.schemes.get(declaration.scheme)
            if existing is not None:
                code = "catalog-scheme-conflict" if existing[1] != declaration.exclusive else "catalog-duplicate"
                issues.append(ValidationIssue(code=code, detail=f"scheme {declaration.scheme!r} is already declared"))
                conflicts.append(existing[0])
                status = (
                    ValidationStatus.CONTRADICTED if existing[1] != declaration.exclusive else ValidationStatus.REJECTED
                )

        elif isinstance(declaration, ClassDeclaration):
            if declaration.scheme not in state.schemes:
                issues.append(
                    ValidationIssue(
                        code="catalog-unknown-scheme",
                        detail=f"scheme {declaration.scheme!r} must be declared before its classes",
                        field="scheme",
                    )
                )
                status = ValidationStatus.REJECTED
            elif existing_class := state.class_by_label.get((declaration.scheme, declaration.label)):
                issues.append(
                    ValidationIssue(code="catalog-duplicate", detail="this catalog class is already declared")
                )
                conflicts.append(existing_class)
                status = ValidationStatus.REJECTED

        elif isinstance(declaration, HierarchyDeclaration):
            parent = state.classes.get(declaration.broader)
            child = state.classes.get(declaration.narrower)
            if parent is None or child is None:
                issues.append(
                    ValidationIssue(
                        code="catalog-unknown-class",
                        detail="both hierarchy endpoints must be declared classes",
                    )
                )
                status = ValidationStatus.REJECTED
            elif declaration.broader == declaration.narrower:
                issues.append(ValidationIssue(code="catalog-cycle", detail="a class cannot be broader than itself"))
                status = ValidationStatus.REJECTED
            elif parent[0] != child[0]:
                issues.append(
                    ValidationIssue(code="catalog-cross-scheme", detail="a hierarchy edge must stay within one scheme")
                )
                status = ValidationStatus.REJECTED
            elif declaration.narrower in state.children.get(declaration.broader, set()):
                existing_id = HierarchyDeclaration(broader=declaration.broader, narrower=declaration.narrower).block_id
                issues.append(ValidationIssue(code="catalog-duplicate", detail="this hierarchy edge already exists"))
                conflicts.append(existing_id)
                status = ValidationStatus.REJECTED
            elif _reachable(state, declaration.narrower, declaration.broader):
                issues.append(ValidationIssue(code="catalog-cycle", detail="this hierarchy edge would create a cycle"))
                status = ValidationStatus.REJECTED

        elif isinstance(declaration, PlacementDeclaration):
            if canonical is None or declaration.source not in canonical:
                issues.append(
                    ValidationIssue(
                        code="catalog-unknown-source",
                        detail="a placement source must belong to the canonical composition",
                        field="source",
                    )
                )
                status = ValidationStatus.REJECTED
            elif declaration.class_id not in state.classes:
                issues.append(
                    ValidationIssue(
                        code="catalog-unknown-class",
                        detail="a placement target must be a declared catalog class",
                        field="class_id",
                    )
                )
                status = ValidationStatus.REJECTED
            elif existing_placement := state.placements.get((declaration.source, declaration.class_id)):
                issues.append(ValidationIssue(code="catalog-duplicate", detail="this source placement already exists"))
                conflicts.append(existing_placement)
                status = ValidationStatus.REJECTED
            else:
                scheme = state.classes[declaration.class_id][0]
                _scheme_id, exclusive = state.schemes[scheme]
                if exclusive:
                    competing = [
                        state.placements[(declaration.source, class_id)]
                        for class_id in state.source_classes.get(declaration.source, set())
                        if class_id != declaration.class_id and state.classes[class_id][0] == scheme
                    ]
                    if competing:
                        issues.append(
                            ValidationIssue(
                                code="catalog-exclusive-conflict",
                                detail=f"source already has another class in exclusive scheme {scheme!r}",
                            )
                        )
                        conflicts.extend(competing)
                        status = ValidationStatus.CONTRADICTED

        if status is ValidationStatus.VALIDATED:
            accepted.append(block)
            state.add_block(block)
            if isinstance(declaration, PlacementDeclaration):
                placements.append(declaration)
        verdicts.append(
            CatalogVerdict(
                index=index,
                kind=declaration.kind,
                status=status,
                block_id=block.block_id,
                issues=issues,
                conflicts_with=_sorted_ids(conflicts),
            )
        )
    return verdicts, accepted, placements


def _reachable(state: _CatalogState, start: BlockId, target: BlockId) -> bool:
    frontier = [start]
    seen: set[BlockId] = set()
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(state.children.get(current, set()))
    return False


def evidence_sources(block_id: BlockId, block: Block) -> set[BlockId]:
    """Canonical source identities represented by a query candidate."""
    if block.MEMORY_TYPE is MemoryType.CANONICAL:
        return {block_id}
    return set(getattr(block, "evidence", None) or [])


def declaration_from_block(block: Block) -> CatalogDeclaration | None:
    """Recover the SDK declaration represented by a catalog semantic block."""
    if not isinstance(block, SemanticBlockV3):
        return None
    if block.kind is SemanticKind.SCHEME:
        assert block.scheme is not None
        assert block.exclusive is not None
        return SchemeDeclaration(scheme=block.scheme, exclusive=block.exclusive)
    if block.kind is SemanticKind.CLASS:
        assert block.scheme is not None
        assert block.label is not None
        return ClassDeclaration(scheme=block.scheme, label=block.label)
    if block.kind is not SemanticKind.RELATION or not block.relations:
        return None
    predicates = {relation.predicate: relation.target for relation in block.relations}
    if set(predicates) == {"broader", "narrower"}:
        return HierarchyDeclaration(broader=predicates["broader"], narrower=predicates["narrower"])
    if set(predicates) == {"classified_as"} and block.evidence:
        return PlacementDeclaration(source=block.evidence[0], class_id=predicates["classified_as"])
    return None


def _sorted_ids(values: Iterable[BlockId]) -> list[BlockId]:
    return sorted(values, key=str)
