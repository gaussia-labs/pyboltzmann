"""What a retained root still needs, which is what pruning must not touch.

A brain retains a set of roots -- tagged releases plus recent snapshots. After drops, former leaves are
unreachable from those roots and can be reclaimed by mark-and-sweep (paper Section 10.4).

Reachability is computed over what a snapshot *names*, transitively, and that is more than the block
ids: a snapshot names each module's composition document, a composition names its blocks, and a canonical
block names the observed bytes it describes. Reclaiming a source blob because no composition listed its
digest directly would destroy the evidence a retained root still points at.

Pruning never decides *what* to forget. A drop already did. This only answers what nothing needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.blocks.canonical import CanonicalBlock
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.module.composition import Composition

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.identity.digest import BlockId, Digest, OciDigest
    from boltzmann.module.snapshot import Snapshot
    from boltzmann.store.base import BlockStore


def reachable_from(snapshot: Snapshot, store: BlockStore) -> set[str]:
    """
    Every digest one snapshot needs, transitively, as hex.

    Hex rather than typed digests because the same bytes can be named at two levels -- a canonical
    block's ``blob`` is an ``OciDigest`` while a block id is a ``BlockId`` -- and reachability is a
    question about bytes, not about what they mean.

    Args:
        snapshot (Snapshot): The retained version.
        store (BlockStore): Where the compositions and blocks are read from.

    Returns:
        set[str]: The hex digests this snapshot keeps alive. Anything it names but cannot read is
        skipped rather than raising, because a tombstoned or already-pruned blob must not stop a sweep.
    """
    keep: set[str] = {snapshot.digest.hex}
    if snapshot.parent is not None:
        # The parent document itself, so the chain an audit walks stays readable. Its *contents* are
        # only kept alive if the parent is itself retained.
        keep.add(snapshot.parent.hex)

    for memory_type, reference in snapshot.modules.items():
        keep.add(reference.composition.hex)
        composition = _read_composition(reference.composition, store)
        if composition is None:
            continue
        for block_id in composition.block_ids:
            keep.add(block_id.hex)
            if memory_type is MemoryType.CANONICAL:
                keep.update(_bytes_named_by(block_id, store))
    return keep


def mark(snapshots: Iterable[Snapshot], store: BlockStore) -> set[str]:
    """
    Every digest any retained snapshot needs.

    Args:
        snapshots (Iterable[Snapshot]): The retained versions.
        store (BlockStore): Where the compositions and blocks are read from.

    Returns:
        set[str]: The union of what they keep alive. Everything else in the store is garbage.
    """
    keep: set[str] = set()
    for snapshot in snapshots:
        keep |= reachable_from(snapshot, store)
    return keep


def sweep(keep: set[str], store: BlockStore) -> list[OciDigest]:
    """
    What the store holds that nothing retained needs.

    Args:
        keep (set[str]): The marked set, as hex.
        store (BlockStore): The store to sweep.

    Returns:
        list[OciDigest]: The reclaimable digests, in a stable order.
    """
    return sorted(
        (digest for digest in store.iter_digests() if digest.hex not in keep),
        key=lambda digest: digest.hex,
    )


def _read_composition(digest: OciDigest, store: BlockStore) -> Composition | None:
    try:
        return Composition.from_document(store.get_bytes(digest))
    except Exception:
        return None


def _bytes_named_by(block_id: BlockId, store: BlockStore) -> set[str]:
    """The observed bytes and normalized view a canonical block describes."""
    try:
        block = store.get_block(block_id)
    except Exception:
        return set()
    if not isinstance(block, CanonicalBlock):
        return set()

    named: set[Digest] = {block.blob}
    if block.normalized_view is not None:
        named.add(block.normalized_view.blob)
    return {digest.hex for digest in named}
