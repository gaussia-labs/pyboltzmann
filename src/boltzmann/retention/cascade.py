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
from boltzmann.retention.requests import CascadePlan

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.identity.digest import BlockId
    from boltzmann.module.ledger import Ledger
    from boltzmann.module.module import Module


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
    found: dict[MemoryType, set[BlockId]] = {}
    for memory_type in (MemoryType.SEMANTIC, MemoryType.PROCEDURAL):
        module = modules.get(memory_type)
        if module is None:
            continue
        for block_id in module.block_ids:
            if not module.store.is_resolvable(block_id):
                continue
            if origin in _references(module.get(block_id)):
                found.setdefault(memory_type, set()).add(block_id)
    return found


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

    Returns:
        CascadePlan: What would be dropped, by module, and what could be re-derived.
    """
    privileged = memory_type is MemoryType.CANONICAL
    dependents: dict[MemoryType, set[BlockId]] = {}

    for dependent in ledger.closure(origin):
        for kind, module in modules.items():
            if dependent in module:
                dependents.setdefault(kind, set()).add(dependent)
                break

    for kind, referencing in structural_dependents(origin, modules).items():
        dependents.setdefault(kind, set()).update(referencing)

    # A structural dependent's own dependents go too, or a drop would leave a dangling reference one
    # hop further out.
    frontier = {block_id for blocks in dependents.values() for block_id in blocks}
    while frontier:
        discovered: set[BlockId] = set()
        for block_id in frontier:
            for kind, referencing in structural_dependents(block_id, modules).items():
                fresh = {found for found in referencing if found not in _flatten(dependents) and found != origin}
                if fresh:
                    dependents.setdefault(kind, set()).update(fresh)
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
    plans = [plan_cascade(origin, memory_type, modules, ledger, rederive_against) for origin in ordered]

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
