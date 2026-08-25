"""Positional facts: which trust root is in force where, and what descends from what.

The hash structure supplies the ordering revocation needs: snapshots are chained by digest, so a
snapshot's position cannot be forged without changing every descendant's parent digest. Validity
is therefore expressed *positionally* rather than temporally -- no clock is consulted anywhere in
this package -- and this module answers the positional questions everything else asks.

Every rule that *derives authorization* walks **first parents only**. The first parent is the
history a reconciliation was performed onto, and no authorization is derived from merged-in
parents (paper Section 12.1). Revocation reachability (:func:`descends_from`) is the one
exception: it walks **every** parent, because a compromise that arrived through a merge -- or
that hides behind an unresolvable one -- must never read as a cleared one.

One step decides the trust root in force: it is the one the first parent names (paper Section
8.9, Case 1). Never search deeper -- if the parent carries no trust root, the brain genuinely has
none at that position, and looking further back would resurrect a key list that a possibly
unapproved removal took out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from boltzmann.authenticity.trust_root import TrustRoot
from boltzmann.exceptions import SnapshotError
from boltzmann.identity.digest import OciDigest
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.base import BlockStore

MAX_WALK = 100_000
"""Most snapshots a walk will visit. A cycle cannot occur in honestly hash-linked documents, but
the walker reads attacker-supplied ones, and a walk must terminate on hostile input too."""


class SnapshotRole(StrEnum):
    """What a snapshot is, relative to its first parent's trust root."""

    GENESIS = "genesis"
    """No parents: the origin of every authority the brain will ever have. The single point in
    the protocol where authority is asserted rather than derived."""

    ORDINARY = "ordinary"
    """The trust root is its first parent's, unchanged. The common case."""

    REVISION = "revision"
    """The trust-root digest differs from the first parent's: a governance act, judged under the
    quorum rule against the *previous* key list."""


@dataclass(frozen=True, slots=True)
class Position:
    """
    One snapshot located in its chain, with the authority context verification needs.

    Attributes:
        snapshot (Snapshot): The located document.
        digest (OciDigest): Its identity.
        parent (Snapshot | None): Its first parent's document; ``None`` for a genesis, and also
            when the chain is truncated -- ``truncated`` tells the two apart.
        role (SnapshotRole): What this snapshot is, relative to its parent's trust root.
        in_force (TrustRoot | None): The trust root this snapshot's signatures are judged
            against. Always the first parent's -- a revision's own signatures answer to the list
            it is *replacing* -- except for a genesis, which answers to its own, because there is
            nothing earlier.
        truncated (bool): Whether the first parent is named but not held. A truncated position
            can never reach ``authorized``: the trust root in force is unknowable.
    """

    snapshot: Snapshot
    digest: OciDigest
    parent: Snapshot | None
    role: SnapshotRole
    in_force: TrustRoot | None
    truncated: bool


def load_snapshot(store: BlockStore, digest: OciDigest) -> Snapshot | None:
    """
    Read a snapshot document by digest, or ``None`` when it is not held or not readable.

    Absence is data here, not an error: a truncated chain is a legitimate state the verifier
    reports, so the walker absorbs unreadability instead of raising it.

    Args:
        store (BlockStore): Where snapshot documents live.
        digest (OciDigest): The document to read.

    Returns:
        Snapshot | None: The document, or ``None``.
    """
    if not store.is_resolvable(digest):
        return None
    try:
        return Snapshot.model_validate_json(store.get_bytes(digest))
    except ValueError:
        return None


def locate(store: BlockStore, snapshot: Snapshot) -> Position:
    """
    Establish a snapshot's role and the trust root in force at its position.

    Args:
        store (BlockStore): Where the parent document is read from.
        snapshot (Snapshot): The document to locate. The caller holds it already; this never
            re-reads it.

    Returns:
        Position: The located snapshot.
    """
    digest = snapshot.digest
    first_parent = snapshot.first_parent
    if first_parent is None:
        return Position(
            snapshot=snapshot,
            digest=digest,
            parent=None,
            role=SnapshotRole.GENESIS,
            in_force=snapshot.trust_root,
            truncated=False,
        )
    parent = load_snapshot(store, first_parent)
    if parent is None:
        return Position(
            snapshot=snapshot,
            digest=digest,
            parent=None,
            role=SnapshotRole.ORDINARY,
            in_force=None,
            truncated=True,
        )
    parent_authority = parent.trust_root.digest if parent.trust_root else None
    child_authority = snapshot.trust_root.digest if snapshot.trust_root else None
    role = SnapshotRole.ORDINARY if parent_authority == child_authority else SnapshotRole.REVISION
    return Position(
        snapshot=snapshot,
        digest=digest,
        parent=parent,
        role=role,
        in_force=parent.trust_root,
        truncated=False,
    )


def walk_first_parents(store: BlockStore, snapshot: Snapshot) -> list[Position]:
    """
    Every position from a snapshot back toward its genesis, nearest first.

    Stops at the genesis, at a truncation, or -- on hostile input -- at :data:`MAX_WALK`.

    Args:
        store (BlockStore): Where parent documents are read from.
        snapshot (Snapshot): Where to start.

    Returns:
        list[Position]: The chain as far as it resolves. The last entry is a genesis exactly
        when the chain is complete; otherwise its ``truncated`` flag says why the walk stopped.

    Raises:
        SnapshotError: If the walk exceeds :data:`MAX_WALK` positions or revisits a digest,
            neither of which an honestly hash-linked chain can produce.
    """
    positions: list[Position] = []
    seen: set[str] = set()
    current = snapshot
    while True:
        position = locate(store, current)
        if position.digest.hex in seen:
            raise SnapshotError(
                f"snapshot {position.digest.short} appears twice in its own ancestry; hash-linked "
                f"documents cannot cycle, so this chain was manufactured"
            )
        seen.add(position.digest.hex)
        if len(seen) > MAX_WALK:
            raise SnapshotError(f"ancestry exceeds {MAX_WALK} snapshots; refusing to walk further")
        positions.append(position)
        if position.parent is None:
            return positions
        current = position.parent


def observed_revisions(store: BlockStore, snapshot: Snapshot) -> list[TrustRoot]:
    """
    Every distinct trust root observable from a snapshot, walking first parents.

    This is the material ``since``-confirmability judges against: a key claiming admission
    earlier than these revisions support is refuted (paper Section 8.5).

    Args:
        store (BlockStore): Where parent documents are read from.
        snapshot (Snapshot): Where to start. Its own trust root is included.

    Returns:
        list[TrustRoot]: Distinct trust roots, nearest first.
    """
    revisions: list[TrustRoot] = []
    known: set[str] = set()
    for position in walk_first_parents(store, snapshot):
        authority = position.snapshot.trust_root
        if authority is not None and authority.digest.hex not in known:
            known.add(authority.digest.hex)
            revisions.append(authority)
    return revisions


def descends_from(store: BlockStore, snapshot: Snapshot, target: OciDigest) -> bool | None:
    """
    Whether ``target`` lies anywhere in a snapshot's ancestry, itself included.

    This is what gives ``compromised_from`` its meaning: a compromise recorded at a position
    withdraws every signature at that position and after it. Unlike authorization, which is
    derived along first parents only, this walks **every** parent: a compromise merged in
    through a reconciliation still happened, and clearing a key because the walk looked down
    only one branch would fail open.

    Args:
        store (BlockStore): Where parent documents are read from.
        snapshot (Snapshot): Where to start.
        target (OciDigest): The position being asked about.

    Returns:
        bool | None: ``True`` when the target is reachable through any parent path, ``False``
        when every path closed at a genesis without finding it, and ``None`` when any named
        parent could not be read first -- fail-closed material: an undecidable compromise is
        not a cleared one.

    Raises:
        SnapshotError: If the ancestry exceeds :data:`MAX_WALK` snapshots, which honestly
            hash-linked documents cannot produce.
    """
    if snapshot.digest == target:
        return True
    seen: set[OciDigest] = {snapshot.digest}
    frontier: list[Snapshot] = [snapshot]
    undecided = False
    while frontier:
        current = frontier.pop()
        for parent in current.parents:
            if parent == target:
                return True
            if parent in seen:
                # A diamond: reconciliations legally reconverge, so a revisit is skipped, not
                # raised -- the digest was already judged once.
                continue
            seen.add(parent)
            if len(seen) > MAX_WALK:
                raise SnapshotError(f"ancestry exceeds {MAX_WALK} snapshots; refusing to walk further")
            document = load_snapshot(store, parent)
            if document is None:
                # A named parent that cannot be read: the target may hide behind it, so the
                # answer is undecidable unless some other path still finds it.
                undecided = True
                continue
            frontier.append(document)
    return None if undecided else False
