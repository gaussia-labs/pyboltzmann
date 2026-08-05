"""What a retained root still needs, which is what pruning must not touch.

A brain retains a set of roots -- tagged releases plus recent snapshots. After drops, former leaves are
unreachable from those roots and can be reclaimed by mark-and-sweep (paper Section 10.4).

Reachability is computed over what a snapshot *names*, transitively, and that is more than the block
ids: a snapshot names each module's composition document, a composition names its blocks, and a block
names whatever content it keeps in the store rather than in its payload. Reclaiming those bytes because
no composition listed their digest directly would destroy what a retained root still points at.

Every block is asked what it names, in every module. Keying that on the canonical type would mean a
sweep that silently deletes the content of any other schema that starts naming bytes -- the failure
mode being data loss on a call whose whole promise is that it only reclaims what nothing needs.

Pruning never decides *what* to forget. A drop already did. This only answers what nothing needs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.exceptions import BlockNotFoundError, BlockTombstonedError
from boltzmann.module.composition import Composition

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.identity.digest import BlockId, OciDigest
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
        set[str]: The hex digests this snapshot keeps alive. Anything it names that was tombstoned
        or already pruned is skipped rather than raising, because bytes that are gone must not stop
        a sweep.

    Raises:
        BlockIntegrityError: If a block is present but its bytes do not hash to the identity they
            are filed under. Corruption is not absence, and reading it as absence would drop that
            block's content from the marked set and let the sweep reclaim it.
        BlockSchemaError: If a present block cannot be decoded, for the same reason.
    """
    keep: set[str] = {snapshot.digest.hex}
    if snapshot.parent is not None:
        # The parent document itself, so the chain an audit walks stays readable. Its *contents* are
        # only kept alive if the parent is itself retained.
        keep.add(snapshot.parent.hex)

    for reference in snapshot.modules.values():
        keep.add(reference.composition.hex)
        composition = _read_composition(reference.composition, store)
        if composition is None:
            continue
        for block_id in composition.block_ids:
            keep.add(block_id.hex)
            # Asked of every module, not only canonical: a block of any type may name its content, and a
            # blob this misses is a blob the sweep deletes while a retained root still names it.
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


def reachable_from_tags(store: BlockStore) -> set[str]:
    """
    Every digest the layout's own tags need.

    A snapshot names knowledge. It does not name the *artifact* built from that knowledge: the manifest, and
    the packed layer per module. Those are named by ``index.json``, which is the other root a layout has --
    a tag is a reference, exactly as a retained snapshot is.

    Without this, packing an artifact and then pruning leaves the layout claiming a tag whose manifest is
    gone: an OCI tool reading ``index.json`` follows the descriptor and finds nothing. The bytes were
    reclaimed because no snapshot mentioned them, which was true and beside the point.

    Only what the tags name *now* is kept. Publishing the same tag twice replaces its entry, so the manifest
    it used to name becomes collectable, which is what should happen.

    Args:
        store (BlockStore): The store to read. One with no layout index contributes nothing.

    Returns:
        set[str]: The manifests the tags name, with their configs and layers, as hex.
    """
    from boltzmann.distribution.manifest import published_artifacts

    keep: set[str] = set()
    for artifact in published_artifacts(store):
        if not store.is_resolvable(artifact.digest):
            continue
        keep.add(artifact.digest.hex)
        if artifact.manifest is None:
            # A manifest this client cannot read is still a manifest a tag names, so its bytes stay. What it
            # points at cannot be followed, and guessing would be worse than keeping one blob too few.
            continue
        keep.add(artifact.manifest.config.digest.hex)
        keep.update(layer.digest.hex for layer in artifact.manifest.layers)
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
    """The composition behind a digest, or ``None`` when the bytes are legitimately gone.

    Only absence and redaction are tolerated. See :func:`_bytes_named_by` for why anything else
    has to propagate rather than be read as "names nothing".
    """
    try:
        return Composition.from_document(store.get_bytes(digest))
    except (BlockNotFoundError, BlockTombstonedError):
        return None


def _bytes_named_by(block_id: BlockId, store: BlockStore) -> set[str]:
    """The content a block names but does not carry, whatever its type.

    A block whose bytes were already pruned or redacted names nothing this sweep can discover,
    and skipping it costs nothing: those bytes are gone either way.

    A block that is *present but unreadable* is a different thing. Corruption is not absence,
    and treating it as absence is how a sweep deletes what a retained root still names -- the
    block's content silently drops out of the marked set, and the one call whose promise is
    that it only reclaims what nothing needs destroys live evidence over a single bit flip. So
    an integrity or schema failure propagates and stops the sweep. That is the conservative
    direction: a prune that refused to run can be run again, and a prune that ran cannot be
    undone.
    """
    try:
        block = store.get_block(block_id)
    except (BlockNotFoundError, BlockTombstonedError):
        return set()
    return {digest.hex for digest in block.content_digests}
