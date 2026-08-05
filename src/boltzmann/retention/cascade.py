"""The provenance cascade: working out what a drop takes with it.

Dropping a block is not always local, and the cascade differs by module (paper Section 10.3).

**Canonical is privileged.** Derived knowledge cites that evidence as its root, so the closure is always
walked and every semantic or procedural block that listed the canonical as evidence is dropped by
default, in the same commit. Re-derivation is not the default: it runs only when the caller registered a
replacement or asks for one explicitly.

**Semantic and procedural cascade too, along a different edge.** The validation gate requires a derived
block's evidence to be canonical, so no derived block cites another through provenance. What links them
is structural: a semantic block's ``relations`` and a procedural step's ``uses``. Those live on the
block, which is what makes this computable without a graph engine -- and what makes dropping a concept
that a procedure depends on visible rather than silent.

The plan is produced before anything is written. That is what lets a policy hold a large cascade for
review instead of discovering its size afterwards, and it is why :func:`plan_cascade` touches no store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.procedural import ProceduralBlock
from boltzmann.blocks.semantic import SemanticBlock
from boltzmann.identity.digest import BlockId
from boltzmann.retention.requests import CascadePlan

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.module.ledger import Ledger
    from boltzmann.module.module import Module


ReferenceIndex = dict[BlockId, dict[MemoryType, set[BlockId]]]
"""Which blocks point at a given block structurally, by module: the inverse of ``_references``."""


def reference_index(modules: dict[MemoryType, Module]) -> ReferenceIndex:
    """
    Invert every structural edge in the brain, in one pass over the blocks.

    A cascade asks "what points at this?" once per origin and once per block the frontier reaches.
    Answering each of those by walking and decoding every semantic and procedural block made a drop
    of k blocks from a module of n cost k*n decodes before anything was written -- 20 000 of them to
    plan a fifty-block drop in a four-hundred-block module. The question is the same shape every
    time, so the answer is computed once and consulted.

    Args:
        modules (dict[MemoryType, Module]): The installed modules.

    Returns:
        ReferenceIndex: Each referenced block mapped to the blocks that point at it, by module.
    """
    index: ReferenceIndex = {}
    for memory_type in (MemoryType.SEMANTIC, MemoryType.PROCEDURAL):
        module = modules.get(memory_type)
        if module is None:
            continue
        for block_id in module.block_ids:
            if not module.store.is_resolvable(block_id):
                continue
            for target in _references(module.get(block_id)):
                index.setdefault(target, {}).setdefault(memory_type, set()).add(block_id)
    return index


def structural_dependents(
    origin: BlockId,
    modules: dict[MemoryType, Module],
) -> dict[MemoryType, set[BlockId]]:
    """
    Blocks that reference ``origin`` through a relation or a step, rather than through provenance.

    Args:
        origin (BlockId): The block being referenced.
        modules (dict[MemoryType, Module]): The installed modules.

    Returns:
        dict[MemoryType, set[BlockId]]: Referencing blocks, by module.
    """
    return {kind: set(blocks) for kind, blocks in reference_index(modules).get(origin, {}).items()}


def _references(block: object) -> set[BlockId]:
    """Every block a block points at structurally, as opposed to cites as evidence."""
    if isinstance(block, SemanticBlock):
        return {relation.target for relation in block.relations or []}
    if isinstance(block, ProceduralBlock):
        return {used for step in block.steps for used in step.uses or []}
    return set()


def plan_cascade(
    origin: BlockId,
    memory_type: MemoryType,
    modules: dict[MemoryType, Module],
    ledger: Ledger,
    rederive_against: BlockId | None = None,
    references: ReferenceIndex | None = None,
) -> CascadePlan:
    """
    Work out what dropping one block would take with it.

    Args:
        origin (BlockId): The block to be dropped.
        memory_type (MemoryType): Which module it belongs to.
        modules (dict[MemoryType, Module]): The installed modules.
        ledger (Ledger): The provenance view, read once by the caller.
        rederive_against (BlockId | None): A replacement canonical block. Dependents are still dropped,
            because a block's citation is part of its identity and one that cites excluded evidence
            cannot stay -- but they are reported as re-derivable so the caller knows what to regenerate
            and against what.
        references (ReferenceIndex | None): The inverted structural edges, when a caller planning
            several drops already built them. Defaults to building them here.

    Returns:
        CascadePlan: What would be dropped, by module, and what could be re-derived.
    """
    privileged = memory_type is MemoryType.CANONICAL
    structural = reference_index(modules) if references is None else references
    dependents: dict[MemoryType, set[BlockId]] = {}

    for dependent in ledger.closure(origin):
        for kind, module in modules.items():
            if dependent in module:
                dependents.setdefault(kind, set()).add(dependent)
                break

    for kind, referencing in structural.get(origin, {}).items():
        dependents.setdefault(kind, set()).update(referencing)

    # A structural dependent's own dependents go too, or a drop would leave a dangling reference one
    # hop further out. ``seen`` tracks what is already in ``dependents``: re-flattening the whole map
    # on every iteration made the walk quadratic in the size of the cascade it was discovering.
    seen = _flatten(dependents)
    frontier = set(seen)
    while frontier:
        discovered: set[BlockId] = set()
        for block_id in frontier:
            for kind, referencing in structural.get(block_id, {}).items():
                fresh = {found for found in referencing if found not in seen and found != origin}
                if fresh:
                    dependents.setdefault(kind, set()).update(fresh)
                    seen |= fresh
                    discovered |= fresh
        frontier = discovered

    rederivable: list[BlockId] = []
    if rederive_against is not None:
        rederivable = sorted(
            (block_id for block_id in _flatten(dependents) if origin in ledger.evidence.get(block_id, [])),
            key=lambda value: value.hex,
        )

    edges = [
        ledger.derivation_records[block_id]
        for block_id in sorted(_flatten(dependents), key=lambda value: value.hex)
        if block_id in ledger.derivation_records
    ]

    return CascadePlan(
        origin=origin,
        origin_memory_type=memory_type,
        privileged=privileged,
        dependents={kind: sorted(blocks, key=lambda value: value.hex) for kind, blocks in dependents.items()},
        rederivable=rederivable,
        provenance_edges=edges,
    )


def plan_many(
    origins: Iterable[BlockId],
    memory_type: MemoryType,
    modules: dict[MemoryType, Module],
    ledger: Ledger,
    rederive_against: BlockId | None = None,
) -> CascadePlan:
    """
    Merge the cascades of several blocks dropped in one commit.

    Args:
        origins (Iterable[BlockId]): The blocks to be dropped.
        memory_type (MemoryType): Which module they belong to.
        modules (dict[MemoryType, Module]): The installed modules.
        ledger (Ledger): The provenance view.
        rederive_against (BlockId | None): A replacement canonical block.

    Returns:
        CascadePlan: The union. ``origin`` names the first block, since a merged plan has no single one;
        the dependents are what matters and they are complete.
    """
    ordered = sorted(set(origins), key=lambda value: value.hex)
    # Built once for the whole batch: the structural edges do not change between origins, and
    # rebuilding them per origin is what made planning a multi-block drop cost a full pass over the
    # brain for each block named.
    structural = reference_index(modules)
    plans = [plan_cascade(origin, memory_type, modules, ledger, rederive_against, structural) for origin in ordered]

    merged: dict[MemoryType, set[BlockId]] = {}
    rederivable: set[BlockId] = set()
    edges: set[BlockId] = set()
    for plan in plans:
        for kind, found in plan.dependents.items():
            merged.setdefault(kind, set()).update(found)
        rederivable.update(plan.rederivable)
        edges.update(plan.provenance_edges)

    # A block being dropped outright is not also a dependent of one.
    dropped = set(ordered)
    merged = {kind: found - dropped for kind, found in merged.items()}

    return CascadePlan(
        origin=ordered[0],
        origin_memory_type=memory_type,
        privileged=memory_type is MemoryType.CANONICAL,
        dependents={kind: sorted(found, key=lambda value: value.hex) for kind, found in merged.items() if found},
        rederivable=sorted(rederivable, key=lambda value: value.hex),
        provenance_edges=sorted(edges, key=lambda value: value.hex),
    )


def _flatten(dependents: dict[MemoryType, set[BlockId]] | dict[MemoryType, list[BlockId]]) -> set[BlockId]:
    return {block_id for blocks in dependents.values() for block_id in blocks}
