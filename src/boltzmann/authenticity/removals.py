"""Verifier-side enforcement of the removal ledger.

A writer recording removals is useful convention; a consumer checking the record is the invariant.
For every block absent relative to a snapshot's first parent, the current provenance composition
must contain a readable :class:`RemovalRecord` naming that block and its module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import ProvenanceBlock, RemovalRecord
from boltzmann.exceptions import BoltzmannError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.store.base import BlockStore


@dataclass(frozen=True, slots=True)
class RemovalIntegrity:
    """The checkable result, including every unaccounted absence and evidence gap."""

    snapshot: OciDigest
    missing_records: dict[MemoryType, tuple[BlockId, ...]] = field(default_factory=dict)
    evidence_gaps: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Whether every absence has a reachable removal record."""
        return not self.missing_records and not self.evidence_gaps

    @property
    def detail(self) -> str:
        """Operator-facing explanation of the violation."""
        parts = [
            f"{memory_type.value}: {', '.join(block.short for block in blocks)}"
            for memory_type, blocks in self.missing_records.items()
        ]
        parts.extend(self.evidence_gaps)
        return "; ".join(parts)


def check_removal_invariant(
    store: BlockStore,
    child: Snapshot,
    parent: Snapshot | None = None,
    *,
    modules: Iterable[MemoryType] | None = None,
) -> RemovalIntegrity:
    """Check every block absent from ``child`` relative to its first parent.

    ``modules`` narrows the comparison only for a local selective installation: its synthetic
    successor intentionally omits modules that were never installed and is not a signed removal.
    Wire snapshots are always checked across the union of child and parent modules.
    """
    if child.first_parent is None:
        return RemovalIntegrity(snapshot=child.digest)

    resolved_parent = parent if parent is not None else _snapshot(store, child.first_parent)
    if resolved_parent is None or resolved_parent.digest != child.first_parent:
        # Immutable v0.7 documents predate the field and may legitimately be verified from a
        # truncated corpus. A modern child, however, has opted into the invariant and cannot use
        # missing history to turn it off.
        if not any(reference.tombstones is not None for reference in child.modules.values()):
            return RemovalIntegrity(snapshot=child.digest)
        return RemovalIntegrity(
            snapshot=child.digest,
            evidence_gaps=(f"first parent {child.first_parent.short} is not resolvable",),
        )

    # Compatibility applies only while both sides are legacy. Once a parent contains the signed
    # field, omitting it from every child reference cannot downgrade verification back to v0.7.
    references = [*resolved_parent.modules.values(), *child.modules.values()]
    if not any(reference.tombstones is not None for reference in references):
        return RemovalIntegrity(snapshot=child.digest)

    selected = set(modules) if modules is not None else set(resolved_parent.modules) | set(child.modules)
    removed: dict[MemoryType, set[BlockId]] = {}
    gaps: list[str] = []
    for memory_type in sorted(selected):
        before_ref = resolved_parent.modules.get(memory_type)
        after_ref = child.modules.get(memory_type)
        if before_ref is None:
            continue
        if after_ref is not None and before_ref.root == after_ref.root:
            continue

        before = _composition(store, before_ref)
        after = _composition(store, after_ref) if after_ref is not None else Composition(memory_type)
        if before is None:
            gaps.append(f"the first parent's {memory_type.value} composition is not resolvable")
            continue
        if after is None:
            gaps.append(f"the child's {memory_type.value} composition is not resolvable")
            continue
        absent = set(before.block_ids) - set(after.block_ids)
        if absent:
            removed[memory_type] = absent

    if not removed:
        return RemovalIntegrity(snapshot=child.digest, evidence_gaps=tuple(gaps))

    recorded = _reachable_removals(store, child)
    missing = {
        memory_type: tuple(sorted(blocks - recorded.get(memory_type, set()), key=lambda value: value.raw))
        for memory_type, blocks in removed.items()
        if blocks - recorded.get(memory_type, set())
    }
    return RemovalIntegrity(snapshot=child.digest, missing_records=missing, evidence_gaps=tuple(gaps))


def _snapshot(store: BlockStore, digest: OciDigest) -> Snapshot | None:
    try:
        return Snapshot.from_document(store.get_bytes(digest))
    except (ValueError, BoltzmannError):
        return None


def _composition(store: BlockStore, reference: ModuleRef) -> Composition | None:
    try:
        composition = Composition.from_document(store.get_bytes(reference.composition))
    except (ValueError, BoltzmannError):
        return None
    if composition.memory_type is not reference.memory_type or composition.root != reference.root:
        return None
    return composition


def _reachable_removals(store: BlockStore, snapshot: Snapshot) -> dict[MemoryType, set[BlockId]]:
    reference = snapshot.modules.get(MemoryType.PROVENANCE)
    if reference is None:
        return {}
    composition = _composition(store, reference)
    if composition is None:
        return {}

    recorded: dict[MemoryType, set[BlockId]] = {}
    for block_id in composition.block_ids:
        if not store.is_resolvable(block_id):
            continue
        try:
            block = store.get_block(block_id)
        except BoltzmannError:
            continue
        if isinstance(block, ProvenanceBlock) and isinstance(block.record, RemovalRecord):
            recorded.setdefault(block.record.memory_type, set()).update(block.record.blocks)
    return recorded
