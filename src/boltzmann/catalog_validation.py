"""Catalog validation translated into the ingestion verdict vocabulary."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.semantic import SemanticBlockV3
from boltzmann.catalog_models import ClassificationRequest, PlacementDeclaration
from boltzmann.catalog_state import load_catalog_state
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.validation import ValidationIssue, ValidationStatus
from boltzmann.module.module import Module


class CatalogVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    index: int = Field(ge=0)
    kind: str
    status: ValidationStatus
    block_id: BlockId
    issues: list[ValidationIssue] = Field(default_factory=list)
    conflicts_with: list[BlockId] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verdicts: list[CatalogVerdict]
    commit: CommitResult

    @property
    def is_clean(self) -> bool:
        return all(verdict.status is ValidationStatus.VALIDATED for verdict in self.verdicts)


def validate_declarations(
    request: ClassificationRequest,
    modules: dict[MemoryType, Module],
    ignore_blocks: set[BlockId] | frozenset[BlockId] = frozenset(),
) -> tuple[list[CatalogVerdict], list[SemanticBlockV3], list[PlacementDeclaration]]:
    """Validate each declaration through its own rule set, sequentially within the batch."""
    state = load_catalog_state(modules, ignore_blocks)
    canonical = modules.get(MemoryType.CANONICAL)
    canonical_ids = set(canonical.block_ids) if canonical is not None else set()
    verdicts: list[CatalogVerdict] = []
    accepted: list[SemanticBlockV3] = []
    placements: list[PlacementDeclaration] = []
    for index, declaration in enumerate(request.declarations):
        problems = declaration.problems(state, canonical_ids)
        status = ValidationStatus.VALIDATED
        if problems:
            status = (
                ValidationStatus.CONTRADICTED
                if any(problem.contradicted for problem in problems)
                else ValidationStatus.REJECTED
            )
        block = declaration.to_block()
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
                issues=[ValidationIssue(code=p.code, detail=p.detail, field=p.field) for p in problems],
                conflicts_with=sorted({item for p in problems for item in p.conflicts_with}, key=str),
            )
        )
    return verdicts, accepted, placements
