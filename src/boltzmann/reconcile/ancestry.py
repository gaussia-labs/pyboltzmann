"""Finding the common ancestor two histories share, and reading a module at an arbitrary snapshot.

What makes reconciliation three-way rather than two-way is the common ancestor, and it is the part that
cannot be skipped (paper Section 12.2). Without it, a block present in one composition and absent from
the other is ambiguous between "they added it" and "I dropped it", and those demand opposite outcomes.
So this module answers one question -- which snapshot did these two histories part from -- and refuses
distinguishably when they never shared one.

**Where the ancestor comes from.** Ancestral snapshot documents do not travel: an artifact publishes the
head as its config blob and one layer per module, so nothing uploads the chain behind it. That sounds
like a problem and is not, because a common ancestor is by definition a snapshot this brain has already
been at. It is found locally or it is not found at all, and "not found at all" is a real answer -- the
histories are unrelated, or the ancestor's document was pruned.
"""

from __future__ import annotations

from collections.abc import Iterable

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import MultipleMergeBasesError, NoCommonAncestorError, SnapshotError
from boltzmann.identity.digest import OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.base import BlockStore


def snapshot_at(store: BlockStore, digest: OciDigest) -> Snapshot:
    """
    Read one snapshot document.

    Args:
        store (BlockStore): Where the document lives.
        digest (OciDigest): Which snapshot to read.

    Returns:
        Snapshot: The document.

    Raises:
        SnapshotError: If the document is not held. A history whose documents are gone cannot be walked,
            and reporting that as an empty history would silently turn a pruned ancestor into "no
            ancestor exists".
    """
    if not store.is_resolvable(digest):
        raise SnapshotError(
            f"snapshot {digest.short} is not resolvable in this store, so the history through it cannot "
            f"be read; it was never fetched, or it was pruned"
        )
    return Snapshot.model_validate_json(store.get_bytes(digest))


def common_ancestor(
    store: BlockStore,
    ours: Iterable[OciDigest],
    theirs: Snapshot,
    theirs_digest: OciDigest,
    hint: OciDigest | None = None,
) -> OciDigest:
    """
    The snapshot two histories parted from.

    Their history is walked outwards from the head and every first common snapshot on a path is collected.
    Candidates that are ancestors of another candidate are discarded. The remaining snapshots are the
    best common ancestors: reconciliation proceeds only when exactly one remains.

    All parents are followed, not just the first. A history that already reconciled something contains
    the sides it merged, and treating those as unrelated would rediscover a divergence that was settled.

    Args:
        store (BlockStore): Where the snapshot documents live.
        ours (Iterable[OciDigest]): Every snapshot our history contains, which is
            :meth:`~boltzmann.brain.Brain.reachable_history`.
        theirs (Snapshot): The head of the other history.
        theirs_digest (OciDigest): Its digest, which is a candidate ancestor itself: if their head is
            already in our history there is nothing to reconcile, and the honest answer is their head.
        hint (OciDigest | None): A snapshot already known to be shared, such as the one
            :class:`~boltzmann.brain.Origin` recorded at pull time. It may bound traversal below that
            point, but never selects the answer by itself.

    Returns:
        OciDigest: The nearest shared snapshot.

    Raises:
        NoCommonAncestorError: If the two histories share nothing. Section 12.2 requires this to be a
            distinguishable failure: there is no three-way merge to compute, and computing a two-way one
            instead would guess at every asymmetry.
        MultipleMergeBasesError: If several incomparable common ancestors remain. The histories must
            reconcile those bases first; selecting one would make the result depend on traversal order.
    """
    contained = set(ours)
    candidates: set[OciDigest] = set()
    seen = {theirs_digest}
    frontier = [(theirs_digest, theirs)]
    while frontier:
        digest, snapshot = frontier.pop()
        if digest in contained:
            candidates.add(digest)
            continue
        for parent in snapshot.parents:
            if parent in seen:
                continue
            seen.add(parent)
            if hint is not None and parent == hint and hint in contained:
                candidates.add(hint)
                continue
            if parent in contained:
                candidates.add(parent)
                continue
            if store.is_resolvable(parent):
                frontier.append((parent, snapshot_at(store, parent)))

    best = {
        candidate
        for candidate in candidates
        if not any(
            other != candidate and _digest_reaches(store, other, candidate, theirs, theirs_digest)
            for other in candidates
        )
    }
    if len(best) == 1:
        return next(iter(best))
    if len(best) > 1:
        named = ", ".join(str(digest) for digest in sorted(best, key=lambda value: value.hex))
        raise MultipleMergeBasesError(
            f"snapshot {theirs_digest.short} and this brain's history have multiple best common "
            f"ancestors: {named}. Reconcile those competing ancestors first so a later reconciliation "
            f"has one unambiguous base."
        )

    raise NoCommonAncestorError(
        f"snapshot {theirs_digest.short} and this brain's history share no ancestor, so there is no "
        f"three-way reconciliation to compute. Either the two brains are unrelated, or the snapshot "
        f"they parted from is no longer held here."
    )


def _digest_reaches(
    store: BlockStore,
    head_digest: OciDigest,
    target: OciDigest,
    theirs: Snapshot,
    theirs_digest: OciDigest,
) -> bool:
    """Whether one candidate descends from another, when its document is available."""
    if head_digest == target:
        return True
    if head_digest == theirs_digest:
        return _reaches(store, theirs, theirs_digest, target)
    if not store.is_resolvable(head_digest):
        return False
    head = snapshot_at(store, head_digest)
    return _reaches(store, head, head_digest, target)


def _reaches(store: BlockStore, head: Snapshot, head_digest: OciDigest, target: OciDigest) -> bool:
    """Whether ``target`` is in the history behind ``head``, following every parent."""
    if head_digest == target:
        return True
    seen = {head_digest}
    frontier = [head]
    while frontier:
        snapshot = frontier.pop()
        for parent in snapshot.parents:
            if parent == target:
                return True
            if parent in seen:
                continue
            seen.add(parent)
            if store.is_resolvable(parent):
                frontier.append(snapshot_at(store, parent))
    return False


def snapshots_between(store: BlockStore, head: Snapshot, head_digest: OciDigest, ancestor: OciDigest) -> list[Snapshot]:
    """
    The snapshots a history added on top of an ancestor, oldest first.

    This is what a rebase replays and what a squash collapses. The order is the first-parent order the
    history recorded, because that is the sequence the work was actually committed in.

    Args:
        store (BlockStore): Where the documents live.
        head (Snapshot): The head of the history.
        head_digest (OciDigest): Its digest.
        ancestor (OciDigest): Where to stop, exclusive.

    Returns:
        list[Snapshot]: The snapshots strictly above ``ancestor``, oldest first. Empty when the head
        *is* the ancestor.
    """
    chain: list[Snapshot] = []
    digest: OciDigest | None = head_digest
    snapshot = head
    while digest is not None and digest != ancestor:
        chain.append(snapshot)
        digest = snapshot.first_parent
        if digest is None or digest == ancestor or not store.is_resolvable(digest):
            break
        snapshot = snapshot_at(store, digest)
    return list(reversed(chain))


def is_reopenable(store: BlockStore, snapshot: Snapshot) -> bool:
    """
    Whether every composition a snapshot names can be read here.

    A published artifact carries the compositions of *one* version -- its head -- so a history's
    intermediate versions arrive as snapshot documents whose composition documents were never
    transferred. Their roots are still verifiable and their memberships are not recoverable: a Merkle root
    commits to a set and cannot be inverted into it.

    This is what a rebase has to ask before promising to preserve the granularity of the history it
    replays. A version it cannot reopen is a version it cannot restate.

    Args:
        store (BlockStore): Where the composition documents live.
        snapshot (Snapshot): The version to check.

    Returns:
        bool: Whether all of its compositions are held.
    """
    return all(store.is_resolvable(reference.composition) for reference in snapshot.modules.values())


def composition_at(store: BlockStore, snapshot: Snapshot, memory_type: MemoryType) -> Composition | None:
    """
    Open one module's composition at an arbitrary snapshot.

    A reconciliation reads three versions of the same module -- the ancestor's and both sides' -- and
    only one of them is the installed one, so this cannot go through
    :meth:`~boltzmann.brain.Brain.module`.

    The stored document is checked against the root the snapshot files it under, the same check the
    installed path makes: a document that does not reproduce its root is refused rather than merged.

    Args:
        store (BlockStore): Where the composition documents live.
        snapshot (Snapshot): The version to read at.
        memory_type (MemoryType): Which module.

    Returns:
        Composition | None: The composition, or ``None`` when that snapshot names no such module.
            Absence is returned rather than raised because a snapshot naming a subset of the modules is
            legitimate -- that is what selective installation produces -- and a reconciliation has to
            tell "this side does not hold the module" apart from "this side emptied it".

    Raises:
        SnapshotError: If the document is missing, or does not reproduce the root it is filed under.
    """
    reference = snapshot.modules.get(memory_type)
    if reference is None:
        return None
    if not store.is_resolvable(reference.composition):
        raise SnapshotError(
            f"the {memory_type.value} composition {reference.composition.short} named by snapshot "
            f"{snapshot.digest.short} is not held, so that version cannot be reopened"
        )
    composition = Composition.from_document(store.get_bytes(reference.composition))
    if composition.root != reference.root:
        raise SnapshotError(
            f"the stored composition for {memory_type.value} has root {composition.root.short} but "
            f"snapshot {snapshot.digest.short} files it under {reference.root.short}"
        )
    return composition
