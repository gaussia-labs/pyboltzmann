"""The required scope set: computed from what a snapshot did, never from what a signature claims.

A verifier MUST compute the scopes a snapshot required from its difference against its first
parent -- or, for a genesis snapshot, against the empty brain -- and MUST reject a signature
whose key does not hold every scope in that set, even if the signature claims fewer (paper
Section 8.5). The ``scopes`` field of a signature record is a statement of intent that aids
diagnosis, never the basis of the decision.

**Fail-closed on incomplete evidence.** Evidence goes missing three ways: the parent snapshot
does not resolve, a composition never travelled, or the provenance module is absent. Each
missing piece becomes a :class:`ScopeQuestion`, and the verdict distinguishes ``scopes`` (what
is certainly required) from ``possible`` (everything that might be). A key is judged fully
authorized only against ``possible``: a verifier that quietly computed a smaller requirement
from a truncated history would be exploitable by shipping a truncated history.

**Redaction is signed state.** Tombstoning changes no Merkle root, so each module reference carries
the destroyed identities its composition still names. Growth of that set is the directly computable
``redact`` trigger. Added provenance remains a compatibility path for snapshots written before the
field existed; unreadable legacy evidence stays fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from boltzmann.authenticity.scopes import Scope
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import ProvenanceBlock, RemovalRecord
from boltzmann.exceptions import BlockError, SnapshotError
from boltzmann.identity.digest import OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.base import BlockStore


class ScopeQuestion(StrEnum):
    """One thing the evidence could not decide, each naming the scopes it leaves open."""

    PARENT_UNRESOLVABLE = "parent_unresolvable"
    """The first parent's document is not held, so the difference cannot be taken at all."""

    CANONICAL_UNREADABLE = "canonical_unreadable"
    """The canonical root changed, but a composition needed to tell gained from lost never
    travelled, so ``ingest`` and ``drop:canonical`` are both open."""

    REDACTION_UNDETERMINED = "redaction_undetermined"
    """Provenance advanced, but its added records could not be read, so a redaction cannot be
    ruled out."""


_QUESTION_SCOPES: dict[ScopeQuestion, frozenset[Scope]] = {
    ScopeQuestion.PARENT_UNRESOLVABLE: frozenset(
        {Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.REDACT, Scope.GOVERN}
    ),
    ScopeQuestion.CANONICAL_UNREADABLE: frozenset({Scope.INGEST, Scope.DROP_CANONICAL}),
    ScopeQuestion.REDACTION_UNDETERMINED: frozenset({Scope.REDACT}),
}
"""What each open question leaves possible. ``propose`` appears nowhere: it is never required."""


@dataclass(frozen=True, slots=True)
class RequiredScopes:
    """
    What a snapshot's difference against its first parent demanded of its signer.

    Attributes:
        scopes (frozenset[Scope]): The scopes certainly required.
        undetermined (frozenset[ScopeQuestion]): What the evidence could not decide.
        parent (OciDigest | None): The first parent the difference was taken against, or
            ``None`` for a genesis snapshot.
    """

    scopes: frozenset[Scope]
    undetermined: frozenset[ScopeQuestion] = field(default=frozenset())
    parent: OciDigest | None = None

    @property
    def is_complete(self) -> bool:
        """Whether every question was decidable, making ``scopes`` the whole answer."""
        return not self.undetermined

    @property
    def possible(self) -> frozenset[Scope]:
        """
        Every scope that might be required, given what could not be decided.

        The fail-closed set: a key is fully authorized only when it holds all of this. A key
        holding ``scopes`` but not ``possible`` is *insufficient evidence*, distinct from both
        valid and insufficient scope.
        """
        opened = (
            frozenset().union(*(_QUESTION_SCOPES[question] for question in self.undetermined))
            if self.undetermined
            else frozenset()
        )
        return self.scopes | opened


@dataclass(frozen=True, slots=True)
class ScopeEvidence:
    """
    Everything :func:`required_scopes` needs, gathered by the caller so the rule stays pure.

    Attributes:
        child (Snapshot): The snapshot whose requirement is being computed.
        parent (Snapshot | None): Its first parent's document. ``None`` means genesis when the
            child names no parent, and *unresolvable* when it names one -- the two demand
            opposite verdicts, and :func:`required_scopes` tells them apart by the child.
        child_compositions (Mapping[MemoryType, Composition | None]): The child's canonical and
            provenance compositions, ``None`` where a named document could not be read.
        parent_compositions (Mapping[MemoryType, Composition | None]): The parent's, same shape.
        added_provenance (tuple[ProvenanceBlock, ...] | None): The provenance blocks the child
            added over the parent, or ``None`` when they could not be established -- which is
            what keeps an unreadable record from silently waiving ``redact``.
    """

    child: Snapshot
    parent: Snapshot | None
    child_compositions: Mapping[MemoryType, Composition | None] = field(default_factory=dict)
    parent_compositions: Mapping[MemoryType, Composition | None] = field(default_factory=dict)
    added_provenance: tuple[ProvenanceBlock, ...] | None = ()


def required_scopes(evidence: ScopeEvidence) -> RequiredScopes:
    """
    Compute the scope set a snapshot's change required.

    Pure over its evidence: no I/O, no store, no configuration. The scope table of paper
    Section 8.5, one row per rule -- which is what lets a property test feed it arbitrary
    compositions and use the table itself as the oracle.

    Args:
        evidence (ScopeEvidence): The two snapshots and what could be read around them.

    Returns:
        RequiredScopes: The requirement, with anything undecidable named rather than dropped.
    """
    child = evidence.child
    parent = evidence.parent

    if parent is None and child.first_parent is not None:
        return RequiredScopes(
            scopes=frozenset(),
            undetermined=frozenset({ScopeQuestion.PARENT_UNRESOLVABLE}),
            parent=child.first_parent,
        )

    scopes: set[Scope] = set()
    questions: set[ScopeQuestion] = set()

    # govern: the trust root digest changed, in either direction -- absent-to-present included.
    parent_authority = parent.trust_root.digest if parent is not None and parent.trust_root else None
    child_authority = child.trust_root.digest if child.trust_root else None
    if child_authority != parent_authority:
        scopes.add(Scope.GOVERN)

    parent_modules = parent.modules if parent is not None else {}
    for memory_type in {**parent_modules, **child.modules}:
        child_ref = child.modules.get(memory_type)
        parent_ref = parent_modules.get(memory_type)
        child_tombstones = set(child_ref.tombstones or ()) if child_ref is not None else set()
        parent_tombstones = set(parent_ref.tombstones or ()) if parent_ref is not None else set()
        if child_tombstones - parent_tombstones:
            scopes.add(Scope.REDACT)
        if child_ref is not None and parent_ref is not None and child_ref.root == parent_ref.root:
            continue
        if memory_type is not MemoryType.CANONICAL:
            # Any change to a non-canonical module -- a new root, an install, an uninstall -- is
            # a commit. Which blocks moved does not matter, so no composition is read.
            scopes.add(Scope.COMMIT)
            continue
        if parent_ref is None:
            # Freshly installed: everything it holds was gained. The block count is committed
            # data inside the signed document; whether it agrees with the composition is the
            # integrity check's question, not this one's.
            if child_ref is not None and child_ref.block_count > 0:
                scopes.add(Scope.INGEST)
        elif child_ref is None:
            if parent_ref.block_count > 0:
                scopes.add(Scope.DROP_CANONICAL)
        else:
            before = evidence.parent_compositions.get(memory_type)
            after = evidence.child_compositions.get(memory_type)
            if before is None or after is None:
                questions.add(ScopeQuestion.CANONICAL_UNREADABLE)
            else:
                delta = before.diff(after)
                if delta.added:
                    scopes.add(Scope.INGEST)
                if delta.removed:
                    scopes.add(Scope.DROP_CANONICAL)

    # Legacy redact: before ModuleRef carried tombstones, only the provenance record betrayed it.
    child_provenance = child.modules.get(MemoryType.PROVENANCE)
    parent_provenance = parent_modules.get(MemoryType.PROVENANCE)
    provenance_advanced = child_provenance is not None and (
        parent_provenance is None or child_provenance.root != parent_provenance.root
    )
    if provenance_advanced and child_provenance is not None and child_provenance.block_count > 0:
        if evidence.added_provenance is None:
            questions.add(ScopeQuestion.REDACTION_UNDETERMINED)
        elif any(
            isinstance(block.record, RemovalRecord) and block.record.mechanism.is_redaction
            for block in evidence.added_provenance
        ):
            scopes.add(Scope.REDACT)

    return RequiredScopes(scopes=frozenset(scopes), undetermined=frozenset(questions), parent=child.first_parent)


def gather_evidence(store: BlockStore, child: Snapshot, parent: Snapshot | None) -> ScopeEvidence:
    """
    Collect what :func:`required_scopes` needs from a store, absorbing every gap into evidence.

    Nothing here raises for a missing document: an unreadable composition becomes ``None`` and
    an unresolvable provenance block makes ``added_provenance`` ``None``, so incompleteness
    reaches the verdict as a named question instead of an exception a caller might swallow.

    Args:
        store (BlockStore): Where compositions and blocks live.
        child (Snapshot): The snapshot whose requirement will be computed.
        parent (Snapshot | None): Its first parent's document, or ``None`` if genesis or not held.

    Returns:
        ScopeEvidence: The evidence, gaps included.
    """
    read = (MemoryType.CANONICAL, MemoryType.PROVENANCE)
    child_compositions = _compositions(store, child, read)
    parent_compositions = _compositions(store, parent, read) if parent is not None else {}
    return ScopeEvidence(
        child=child,
        parent=parent,
        child_compositions=child_compositions,
        parent_compositions=parent_compositions,
        added_provenance=_added_provenance(
            store,
            child,
            parent,
            child_compositions.get(MemoryType.PROVENANCE),
            parent_compositions.get(MemoryType.PROVENANCE),
        ),
    )


def _compositions(
    store: BlockStore, snapshot: Snapshot, wanted: tuple[MemoryType, ...]
) -> dict[MemoryType, Composition | None]:
    """The named compositions that could be read, with ``None`` standing in for each that could not."""
    from boltzmann.reconcile.ancestry import composition_at

    held: dict[MemoryType, Composition | None] = {}
    for memory_type in wanted:
        if memory_type not in snapshot.modules:
            continue
        try:
            held[memory_type] = composition_at(store, snapshot, memory_type)
        except SnapshotError:
            held[memory_type] = None
    return held


def _added_provenance(
    store: BlockStore,
    child: Snapshot,
    parent: Snapshot | None,
    child_composition: Composition | None,
    parent_composition: Composition | None,
) -> tuple[ProvenanceBlock, ...] | None:
    """The provenance blocks the child added, or ``None`` when they cannot be established."""
    child_ref = child.modules.get(MemoryType.PROVENANCE)
    parent_ref = parent.modules.get(MemoryType.PROVENANCE) if parent is not None else None
    if child_ref is None or (parent_ref is not None and child_ref.root == parent_ref.root):
        return ()
    if child_composition is None or (parent_ref is not None and parent_composition is None):
        return None
    carried = set(parent_composition.block_ids) if parent_composition is not None else set()
    added: list[ProvenanceBlock] = []
    for block_id in child_composition.block_ids:
        if block_id in carried:
            continue
        try:
            block = store.get_block(block_id)
        except BlockError:
            return None
        if isinstance(block, ProvenanceBlock):
            added.append(block)
    return tuple(added)
