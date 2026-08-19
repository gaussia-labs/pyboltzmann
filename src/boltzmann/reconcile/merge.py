"""The structural half of reconciliation: set arithmetic over one module.

This is where the architecture pays off against version control (paper Section 12.2). Git merges lines
of text; here what is merged is a set of immutable, content-addressed blocks. Nothing is ever "the same
block, slightly modified" -- modifying a block yields a different identity -- so a textual conflict is
not representable, and reconciling one module is set arithmetic over identifiers, which converges
regardless of the order the sides are combined in.

For each module, with :math:`B` the ancestor's composition and :math:`X`, :math:`Y` the two compositions
being reconciled:

.. math::

    M = (B \\cup X \\cup Y) \\setminus ((B \\setminus X) \\cup (B \\setminus Y))

Everything either side added is kept, and everything either side removed stays removed.

**The result is a candidate, not a commit.** The equation is applied per module and is individually
correct in each, which is precisely why it is not sufficient: the invariants this architecture rests on
run *between* modules, and a set operation never crosses that boundary. See
:mod:`boltzmann.reconcile.gate`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import AppendOnlyViolationError
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.merkle.tree import merkle_root, sorted_leaves

if TYPE_CHECKING:
    from boltzmann.module.composition import Composition


class ModuleReconciliation(BaseModel):
    """
    What reconciling one module produced, and where each part came from.

    Attributes:
        memory_type (MemoryType): The module.
        block_ids (list[BlockId]): The reconciled composition, in canonical leaf order.
        root (MerkleRoot): The root that composition commits to.
        added_by_us (list[BlockId]): Blocks this history added since the ancestor.
        added_by_them (list[BlockId]): Blocks the other history added since the ancestor.
        removed (list[BlockId]): Blocks the ancestor held that the result excludes, because at least one
            side dropped them.
        incoming (list[BlockId]): Blocks entering this brain for the first time -- present in the
            result, absent from our side. These are the ones the validation gate judges; the rest were
            already ours and were already judged when they were committed.
        adopted (bool): Whether the result is the other side's composition taken unchanged, because this
            brain does not hold the module at all. A partial install does not hold every module, and not
            holding one is not the same as having emptied it (paper Section 12.8).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_type: MemoryType
    block_ids: list[BlockId] = Field(default_factory=list)
    root: MerkleRoot
    added_by_us: list[BlockId] = Field(default_factory=list)
    added_by_them: list[BlockId] = Field(default_factory=list)
    removed: list[BlockId] = Field(default_factory=list)
    incoming: list[BlockId] = Field(default_factory=list)
    adopted: bool = False

    @property
    def is_noop(self) -> bool:
        """Whether the reconciled composition is the one this brain already had."""
        return not self.added_by_them and not self.removed and not self.adopted


def merge_module(
    memory_type: MemoryType,
    base: Composition | None,
    ours: Composition | None,
    theirs: Composition | None,
) -> ModuleReconciliation | None:
    """
    Reconcile one module by Equation 1.

    **Module-level absence is never a removal.** A composition that is present but smaller says a block
    was dropped; a module that is absent says this side never installed it, and treating the two alike
    would let a selective install delete the modules it never fetched from the other side's history.
    Only blocks missing from a composition both sides hold count as removals.

    Args:
        memory_type (MemoryType): Which module is being reconciled.
        base (Composition | None): The ancestor's composition. ``None`` when the ancestor did not name
            this module, in which case nothing can have been removed and the result is the union.
        ours (Composition | None): This history's composition.
        theirs (Composition | None): The other history's composition.

    Returns:
        ModuleReconciliation | None: The reconciled module, or ``None`` when neither side holds it.

    Raises:
        AppendOnlyViolationError: If the arithmetic excludes a block from an append-only module. The
            episodic module records what happened, so no conforming history can have dropped from it;
            if one apparently did, its composition is malformed and merging it would launder the
            violation into a new root.
        ValueError: If a composition belongs to a different module than the one being reconciled.
    """
    for name, composition in (("base", base), ("ours", ours), ("theirs", theirs)):
        if composition is not None and composition.memory_type is not memory_type:
            raise ValueError(
                f"the {name} composition is a {composition.memory_type.value} one, but this is "
                f"reconciling {memory_type.value}"
            )

    if ours is None and theirs is None:
        return None

    ancestor = set(base) if base is not None else set()

    if ours is None:
        # Not held here. Adopt the other side's version verbatim: the modules a partial install never
        # fetched take their roots from the other history unchanged (paper Section 12.8).
        assert theirs is not None
        members = set(theirs)
        return ModuleReconciliation(
            memory_type=memory_type,
            block_ids=sorted_leaves(members),
            root=theirs.root,
            added_by_them=sorted_leaves(members - ancestor),
            incoming=sorted_leaves(members),
            adopted=True,
        )

    mine = set(ours)
    if theirs is None:
        # They do not hold it, so there is nothing of theirs to fold in and nothing they can have
        # dropped. Ours stands.
        return ModuleReconciliation(
            memory_type=memory_type,
            block_ids=ours.block_ids,
            root=ours.root,
            added_by_us=sorted_leaves(mine - ancestor),
        )

    yours = set(theirs)
    excluded = (ancestor - mine) | (ancestor - yours)
    members = (ancestor | mine | yours) - excluded

    if memory_type.is_append_only and excluded:
        named = ", ".join(block.short for block in sorted_leaves(excluded))
        raise AppendOnlyViolationError(
            f"reconciling {memory_type.value} would exclude {named}, but that module is append-only: a "
            f"history that dropped from it is malformed, and merging it would write the violation into "
            f"a new root"
        )

    return ModuleReconciliation(
        memory_type=memory_type,
        block_ids=sorted_leaves(members),
        root=merkle_root(sorted_leaves(members)),
        added_by_us=sorted_leaves(mine - ancestor),
        added_by_them=sorted_leaves(yours - ancestor),
        removed=sorted_leaves(excluded),
        incoming=sorted_leaves(members - mine),
    )


def reconciled_modules(
    base: dict[MemoryType, Composition | None],
    ours: dict[MemoryType, Composition | None],
    theirs: dict[MemoryType, Composition | None],
) -> dict[MemoryType, ModuleReconciliation]:
    """
    Reconcile every module either side holds.

    Args:
        base (dict[MemoryType, Composition | None]): The ancestor's compositions.
        ours (dict[MemoryType, Composition | None]): This history's compositions.
        theirs (dict[MemoryType, Composition | None]): The other history's compositions.

    Returns:
        dict[MemoryType, ModuleReconciliation]: One entry per module present on either side, in the
        canonical module order.
    """
    results = {}
    for memory_type in MemoryType:
        merged = merge_module(memory_type, base.get(memory_type), ours.get(memory_type), theirs.get(memory_type))
        if merged is not None:
            results[memory_type] = merged
    return results
