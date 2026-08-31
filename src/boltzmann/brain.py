"""The Brain: the class you instantiate to work with a brain.

A brain is portable data, not a service. This class is a handle onto a store and a snapshot; it
holds no connection, no server, and no model, so opening the same directory twice yields two handles
onto the same knowledge rather than two competing states.

**What it implements.** The operations the protocol defines mechanically: hashing and preserving a
source, running the validation gate, committing atomically with provenance and a Merkle rebuild,
resolving and verifying, and advancing the snapshot. That is code a caller should not have to write.

**What it delegates.** The two things the paper assigns elsewhere. Interpretation -- what knowledge a
source yields -- enters through a :class:`~boltzmann.ingest.proposer.CandidateProposer` the caller
supplies. Retrieval strategy -- which indices to consult and how to rank -- enters through a
:class:`~boltzmann.query.planner.QueryPlanner`. Neither ships here.

**How a commit is atomic.** Content-addressed blobs are written first, and the snapshot pointer moves
last. A failure part-way through leaves orphan blobs that a prune reclaims, and the previous snapshot
still current. There is no state in which a root names a block the store does not hold.
"""

# Catalog validation speaks the ingestion verdict vocabulary, so its import intentionally follows
# ingestion package initialization below.  Keeping that dependency direction avoids an import cycle.
# ruff: noqa: I001

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.authenticity.authenticator import (
    AuthenticationReport,
    Authenticator,
    Authorship,
    AuthorshipState,
    FindingKind,
    SnapshotStance,
)
from boltzmann.authenticity.chain import (
    SnapshotRole,
    descends_from,
    load_snapshot,
    locate,
    observed_revisions,
    walk_first_parents,
)
from boltzmann.authenticity.removals import check_removal_invariant
from boltzmann.authenticity.diff import gather_evidence, required_scopes
from boltzmann.authenticity.governance import RotationPlan, RotationResult
from boltzmann.authenticity.keys import SshPublicKey
from boltzmann.authenticity.pins import PinSource, TrustPin, read_pin, write_pin
from boltzmann.authenticity.policy import UnsignedPolicy, VerificationPolicy
from boltzmann.authenticity.record import (
    SignatureRecord,
    for_snapshot,
    reachable_signatures,
    read_index,
    store_record,
)
from boltzmann.authenticity.scopes import Scope
from boltzmann.authenticity.signers import Signer
from boltzmann.authenticity.sshsig import sign as sshsig_sign
from boltzmann.authenticity.trust_root import SinceVerdict, TrustedKey, TrustRoot, confirm_since
from boltzmann.blocks.base import Block
from boltzmann.blocks.canonical import CanonicalBlock, NormalizedView
from boltzmann.blocks.content import ContentRef, require_media_type
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    DemotionRecord,
    DerivationRecord,
    NormalizationRecord,
    Producer,
    ProducerKind,
    ProvenanceBlock,
    RegistrationRecord,
    RemovalMechanism,
    RemovalRecord,
    SupersessionRecord,
    ValidationRecord,
)
from boltzmann.catalog import (
    Catalog,
    CatalogBrowseResult,
    CatalogDeclaration,
    CatalogPathView,
    ClassificationRequest,
)
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.distribution.layers import pack_history, pack_module, unpack_history, unpack_layer
from boltzmann.distribution.manifest import (
    BrainManifest,
    Descriptor,
    build_manifest,
    build_signature_manifest,
    declare_schema_versions,
    published_artifacts,
    require_supported_schemas,
)
from boltzmann.distribution.media_types import (
    ANNOTATION_EMBEDDING_MODEL,
    ANNOTATION_INDEX_KIND,
    ANNOTATION_MEMORY_TYPE,
    ANNOTATION_SCHEMA_VERSIONS,
    ANNOTATION_SOURCE_SNAPSHOT,
    ARTIFACT_TYPE,
    CONFIG_MEDIA_TYPE,
    EMPTY_CONFIG_BYTES,
    MANIFEST_MEDIA_TYPE,
    PROJECTION_MEDIA_TYPE,
    REF_NAME_ANNOTATION,
    SIGNATURE_MEDIA_TYPE,
    VECTOR_INDEX_MEDIA_TYPE,
)
from boltzmann.distribution.registry import FetchResult, InstallPlan, RegistryClient, RegistryReferrers
from boltzmann.distribution.projection import Projection
from boltzmann.exceptions import (
    AuthenticityError,
    BlockError,
    BlockNotFoundError,
    DistributionError,
    DivergenceError,
    GovernanceConflictError,
    IdentityError,
    InsufficientScopeError,
    NoCommonAncestorError,
    ProtocolError,
    QueryError,
    QuorumFailureError,
    ReconciliationBlockedError,
    ReconciliationError,
    ReconciliationHaltedError,
    ReferenceNotFoundError,
    ResolutionRefusedError,
    RollbackError,
    RemovalInvariantError,
    SnapshotError,
    TrustRootMismatchError,
    UnauthorizedKeyError,
    UnsignedBrainError,
)
from boltzmann.identity.digest import BlockId, Digest, MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize, parse_json_strict
from boltzmann.identity.time import utc_timestamp
from boltzmann.indices.base import Index, IndexKind, TravellingIndex
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.pipelines import get_pipeline
from boltzmann.ingest.proposer import CandidateProposer, CandidateSet
from boltzmann.ingest.register import RegistrationRequest, RegistrationResult
from boltzmann.ingest.schema import candidates_schema as _candidates_schema
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES, ProcessingTask, TaskOperation
from boltzmann.ingest.validation import (
    ValidationAudit,
    ValidationReport,
    ValidationStatus,
    Validator,
    validate,
)
from boltzmann.catalog_validation import ClassificationResult, validate_declarations
from boltzmann.merkle.proof import InclusionProof
from boltzmann.merkle.tree import sorted_leaves
from boltzmann.module.composition import Composition
from boltzmann.module.ledger import Ledger
from boltzmann.module.module import Module
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.query.evidence import EvidenceBundle
from boltzmann.query.planner import QueryPlanner
from boltzmann.query.request import Query
from boltzmann.query.scan import scan
from boltzmann.reconcile.ancestry import (
    common_ancestor,
    composition_at,
    is_reopenable,
    snapshot_at,
    snapshots_between,
)
from boltzmann.reconcile.gate import BlockVerdict, judge_incoming
from boltzmann.reconcile.merge import ModuleReconciliation, reconciled_modules
from boltzmann.reconcile.requests import (
    ReconcilePlan,
    ReconcileRequest,
    ReconcileResult,
    ReconcileStrategy,
)
from boltzmann.reconcile.resolution import (
    ReconcileState,
    ReconcileStatus,
    RemovalAcceptance,
    Resolution,
    ResolutionKind,
)
from boltzmann.reconcile.strategies import attribution_for, attribution_table, merged_parents, replay_steps
from boltzmann.retention.cascade import plan_many
from boltzmann.retention.policy import RetentionPolicy
from boltzmann.retention.reachability import mark, reachable_from_tags, sweep
from boltzmann.retention.requests import (
    CascadePlan,
    DropRequest,
    DropResult,
    ProducerDropRequest,
    PruneReport,
    RedactionResult,
    ResolvabilityReport,
    SupersessionResult,
)
from boltzmann.store.base import BlockStore
from boltzmann.store.oci_layout import OciLayoutStore

HEAD_POINTER = "head"
"""Name of the mutable pointer that says which snapshot is current."""

_UNRECORDED_CHECKS = "unrecorded"
"""Placeholder check identifier for a report built before the gate recorded its check set.

A validation record must name at least one check, because a verdict under an unstated check set is
not something a consumer can act on. A report assembled by hand, or by an older SDK, has no set to
name -- so the record says so in the one way that cannot be mistaken for a check that ran.
"""

MAX_MERGED_PER_CALL = 4096
"""Ceiling on signature records merged from one referrers listing.

A listing is unauthenticated remote input: whoever can attach referrers decides its length, so one
pull's work has to be bounded here rather than by the attacker.
"""

RECONCILE_POINTER = "reconcile"
"""Name of the pointer holding a reconciliation someone is still resolving.

The second and only other piece of mutable state a brain has, and the same device version control uses for a
merge it could not finish on its own. It is not part of any snapshot and never published: it describes an
operation in progress, not a version. Stores already keep pointers by name, so this needs nothing of them
that ``head`` did not already need.
"""


def _contenders(verdict: BlockVerdict) -> set[BlockId]:
    """The competing successors of a precedence question, as the gate named them.

    Read off the verdict rather than recomputed, so the decision is made against exactly the contenders the
    operator was shown.
    """
    from boltzmann.reconcile.gate import PRECEDENCE_CODE

    if not any(issue.code == PRECEDENCE_CODE for issue in verdict.issues):
        return set()
    return set(verdict.conflicts_with)


def _is_supported(record: ProvenanceBlock, members: Mapping[MemoryType, list[BlockId]]) -> bool:
    """Whether a version holds both blocks a precedence edge names.

    A tie-break is a supersession edge, and an edge whose winner or loser is not a member of the version it
    is written into points at nothing. During a replay the losing successor may not have arrived yet, so the
    edge waits for the step that holds both rather than being written early or held back to the end.
    """
    if not isinstance(record.record, SupersessionRecord):
        return True
    held = {block for blocks in members.values() for block in blocks}
    return {record.record.block, record.record.supersedes} <= held


class Origin(BaseModel):
    """
    Where a local brain was pulled from, and the point it started at.

    The remote snapshot is recorded so a later push can tell whether the remote is still an ancestor of
    what is local. Without it, publishing over a tag could overwrite work someone else pushed in the
    meantime, and content addressing would not save you: the blobs would still be there, but no
    retained root would name them.

    Attributes:
        reference (str): Repository reference, such as ``ghcr.io/gaussia-labs/monotributo-brain``.
        tag (str): The tag that was pulled.
        snapshot (OciDigest): The snapshot the remote was on at pull time.
        partial (bool): Whether only some of the artifact's modules were installed. Publishing a partial
            install back over the tag it came from would quietly drop the modules that were never
            fetched, so that particular push is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    snapshot: OciDigest
    partial: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedConfig:
    """A config document resolved to the snapshot that gives it authority."""

    document: Snapshot | Projection
    source: Snapshot

    @property
    def modules(self) -> dict[MemoryType, ModuleRef]:
        """Module references the artifact actually exposes."""
        return self.document.modules

    @property
    def is_projection(self) -> bool:
        """Whether installation is a partial view rather than the source snapshot itself."""
        return isinstance(self.document, Projection) or self.document.digest != self.source.digest


REMOTES_POINTER = "remotes"
"""Name of the pointer remembering which remotes were ever seen authentically signed."""


class RemoteAuthenticity(BaseModel):
    """
    Which remotes this store has seen authentically signed, and at which snapshot first.

    The anti-stripping guard's memory (paper Section 8.10): "previously seen signed" has to
    survive local commits -- the signatures over the current local head say nothing about a
    *remote* once the head moves -- and it has to be per-repository, so signing local work
    never arms the guard against an honestly-unsigned upstream, and a stripper cannot dodge it
    by publishing under a new tag of the same repository.

    Attributes:
        boltzmann (int): Protocol version that wrote this pointer.
        seen_signed (dict[str, OciDigest]): Repository reference to the first snapshot whose
            authorship verified as authorized from that remote. First sighting is kept, never
            overwritten: it is evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    seen_signed: dict[str, OciDigest] = Field(default_factory=dict)


class BrainState(BaseModel):
    """
    The one piece of mutable state a brain has: which snapshot is current.

    Everything else is content-addressed and immutable. Keeping this separate is what makes a commit
    atomic -- blobs go in first, this moves last.

    Attributes:
        boltzmann (int): Protocol version that wrote this pointer.
        snapshot (OciDigest): The current snapshot document.
        retained (list[OciDigest]): Snapshots kept reachable, most recent first. Pruning reclaims
            only what none of these still reference (paper Section 10.4).
        origin (Origin | None): Where this brain was pulled from, if it was.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    snapshot: OciDigest
    retained: list[OciDigest] = Field(default_factory=list)
    origin: Origin | None = None


class Brain:
    """
    A handle onto one brain.

    Attributes:
        store (BlockStore): Where blocks and blobs live.
        actor (Actor): Who this client acts as, recorded in every provenance entry it writes.
        policy (RetentionPolicy): What removals this deployment permits.
        planner (QueryPlanner | None): How queries are planned. ``None`` means the built-in scan.
        indices (dict[MemoryType, list[Index]]): Derived views to maintain, if any.
        validators (Sequence[Validator] | None): Checks to run at the gate. ``None`` means the
            protocol's own set.
    """

    def __init__(
        self,
        store: BlockStore,
        actor: Actor,
        snapshot: Snapshot | None = None,
        policy: RetentionPolicy | None = None,
        planner: QueryPlanner | None = None,
        indices: dict[MemoryType, list[Index]] | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> None:
        """
        Open a brain over a store.

        Args:
            store (BlockStore): Where blocks and blobs live.
            actor (Actor): Who this client acts as.
            snapshot (Snapshot | None): The state to open. Defaults to the store's current snapshot,
                or an empty brain if it has none.
            policy (RetentionPolicy | None): Retention policy. Defaults to the conservative one: no
                canonical drops, no redaction.
            planner (QueryPlanner | None): Query planner to use.
            indices (dict[MemoryType, list[Index]] | None): Indices to rebuild on commit.
            validators (Sequence[Validator] | None): Checks to run at the validation gate.
        """
        self.store = store
        self.actor = actor
        self.policy = policy if policy is not None else RetentionPolicy()
        self.planner = planner
        self.indices = dict(indices or {})
        self.validators = validators
        self._state = self._read_state()
        self._snapshot = snapshot if snapshot is not None else self._load_snapshot()
        self._modules: dict[MemoryType, Module] = {}
        self._authorship_cache: tuple[tuple[str, tuple[OciDigest, ...]], Authorship] | None = None
        self._vouched: set[MemoryType] = set()
        """Memory types whose travelling index this brain built or loaded, and may therefore publish.

        A travelling index cannot be regenerated -- that is what ``rebuildable = False`` means -- so an
        index this brain never populated holds nothing, and dumping it would publish a layer that claims a
        vector index and carries none.
        """

        # A structural index is rebuilt on every write, so a brain that committed in this process has one
        # that matches. A brain that was merely opened would not, and an empty index does not announce
        # itself: a planner consulting it gets no candidates and reports a confident nothing. Rebuilding
        # here costs what one commit costs, and makes "the index reflects the installed version" true on
        # every path rather than only on the write path.
        self.rebuild_indices()
        self._restore_travelling()

    @classmethod
    def open(
        cls,
        path: Path | str,
        actor: Actor,
        policy: RetentionPolicy | None = None,
        planner: QueryPlanner | None = None,
        indices: dict[MemoryType, list[Index]] | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> Brain:
        """
        Open or create a brain in a directory.

        The directory is an OCI Image Layout, so the same path can be published without conversion.

        Args:
            path (Path | str): Directory holding the layout.
            actor (Actor): Who this client acts as.
            policy (RetentionPolicy | None): Retention policy.
            planner (QueryPlanner | None): Query planner to use.
            indices (dict[MemoryType, list[Index]] | None): Indices to rebuild on commit.
            validators (Sequence[Validator] | None): Checks to run at the validation gate.

        Returns:
            Brain: The opened brain, at whichever snapshot the directory was left on.
        """
        return cls(
            OciLayoutStore(Path(path)),
            actor=actor,
            policy=policy,
            planner=planner,
            indices=indices,
            validators=validators,
        )

    @classmethod
    def init(
        cls,
        path: Path | str,
        actor: Actor,
        trust_root: TrustRoot,
        signers: Sequence[Signer] = (),
        labels: dict[str, str] | None = None,
        policy: RetentionPolicy | None = None,
        planner: QueryPlanner | None = None,
        indices: dict[MemoryType, list[Index]] | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> Brain:
        """
        Create a brain: a genesis snapshot with no parents and its first trust root.

        The origin of every authority the brain will ever have, and the single point in the
        protocol where authority is asserted rather than derived (paper Section 8.7). What gives
        it meaning happens outside it: consumers pin the trust root's digest on first contact --
        a genesis is not validated, it is anchored.

        The genesis SHOULD satisfy the quorum its own trust root declares. This proves nothing
        against an attacker, who can satisfy it just as easily; it is required for coherence -- a
        brain declaring a quorum of two whose founding act carried one signature is stating a
        rule it has already departed from. A genesis below its declared quorum is logged as a
        warning, never refused.

        Args:
            path (Path | str): Directory for the new brain's layout.
            actor (Actor): Who this client acts as.
            trust_root (TrustRoot): The first key list, at whatever revision it declares.
            signers (Sequence[Signer]): Who signs the founding act. Empty produces an unsigned
                genesis -- the zero-configuration case, which stays fully verifiable for
                integrity but can never retroactively acquire a signed history.
            labels (dict[str, str] | None): Optional annotations for the genesis.
            policy (RetentionPolicy | None): Retention policy for the handle returned.
            planner (QueryPlanner | None): Query planner for the handle returned.
            indices (dict[MemoryType, list[Index]] | None): Indices to rebuild on commit.
            validators (Sequence[Validator] | None): Checks to run at the validation gate.

        Returns:
            Brain: The new brain, at its genesis.

        Raises:
            SnapshotError: If the directory already holds a brain. A brain has exactly one
                genesis, and every other snapshot must be reachable from it.
        """
        store = OciLayoutStore(Path(path))
        if store.read_pointer(HEAD_POINTER):
            raise SnapshotError(
                f"{path} already holds a brain; a brain has exactly one genesis, and a second "
                f"would be a second brain in the same directory"
            )
        brain = cls(
            store,
            actor=actor,
            policy=policy,
            planner=planner,
            indices=indices,
            validators=validators,
        )
        genesis = Snapshot(trust_root=trust_root, labels=labels)
        brain._advance(genesis)
        distinct: set[bytes] = set()
        for signer in signers:
            brain.sign(signer)
            distinct.add(signer.public_key.blob)
        if len(distinct) < trust_root.govern_quorum:
            logging.getLogger(__name__).warning(
                "genesis of %s declares a govern quorum of %d and carries %d signature(s); the founding "
                "act departs from the rule the brain itself states",
                path,
                trust_root.govern_quorum,
                len(distinct),
            )
        cls._warn_without_governance_margin(trust_root, path)
        return brain

    @staticmethod
    def _warn_without_governance_margin(trust_root: TrustRoot, where: object) -> None:
        """Say so at the moment the margin is chosen, not the moment it is needed.

        A quorum equal to the number of govern holders is not an error and no rule forbids it, so
        this is a warning rather than a refusal. It is emitted here, at creation and at revision,
        because that is the only moment anything can still be done about it: once the key is lost
        the protocol has no recovery path, and a report noticing it afterwards arrives too late to
        inform the decision it was about.
        """
        if trust_root.has_governance_margin:
            return
        logging.getLogger(__name__).warning(
            "trust root of %s sets a govern quorum of %d with %d govern holder(s): losing one key "
            "freezes governance permanently, with no recovery path inside the protocol. Keep more "
            "govern holders than the quorum requires",
            where,
            trust_root.govern_quorum,
            len(trust_root.govern_holders),
        )

    def __repr__(self) -> str:
        installed = ", ".join(kind.value for kind in self._snapshot.installed) or "empty"
        return f"Brain({installed}, blocks={self._snapshot.block_count})"

    # --- State ----------------------------------------------------------------

    def _read_state(self) -> BrainState | None:
        raw = self.store.read_pointer(HEAD_POINTER)
        return BrainState.model_validate(parse_json_strict(raw)) if raw else None

    def _read_remotes(self) -> RemoteAuthenticity:
        raw = self.store.read_pointer(REMOTES_POINTER)
        return RemoteAuthenticity.model_validate(parse_json_strict(raw)) if raw else RemoteAuthenticity()

    def _record_seen_signed(self, reference: str, snapshot: OciDigest) -> None:
        """Remember that this repository was seen authentically signed, once, forever."""
        remotes = self._read_remotes()
        if reference in remotes.seen_signed:
            return
        updated = remotes.model_copy(update={"seen_signed": {**remotes.seen_signed, reference: snapshot}})
        self.store.write_pointer(REMOTES_POINTER, canonicalize(updated.model_dump(mode="json", exclude_none=True)))

    def _load_snapshot(self) -> Snapshot:
        if self._state is None:
            return Snapshot()
        return Snapshot.from_document(self.store.get_bytes(self._state.snapshot))

    def _advance(
        self,
        snapshot: Snapshot,
        origin: Origin | None = None,
        retain: Iterable[OciDigest] = (),
    ) -> Snapshot:
        """Write the snapshot document, then move the pointer. Order matters for atomicity.

        ``retain`` names snapshots that must stay reachable alongside the new one. A merge uses it to keep
        the history it merged in: reachability for pruning is computed from the retained set, and a
        contribution whose snapshots were reclaimed would leave a lineage pointing at documents no audit
        can resolve -- which is the guarantee that only merge keeps the other side's snapshots.
        """
        digest = self.store.put_bytes(snapshot.canonical_bytes())
        retained = [digest, *retain, *(self._state.retained if self._state else [])]
        deduplicated: list[OciDigest] = []
        for candidate in retained:
            if candidate not in deduplicated:
                deduplicated.append(candidate)
        state = BrainState(
            snapshot=digest,
            retained=deduplicated[: self.policy.retained_roots],
            origin=origin if origin is not None else (self._state.origin if self._state else None),
        )
        self.store.write_pointer(HEAD_POINTER, canonicalize(state.model_dump(mode="json", exclude_none=True)))
        self._state = state
        self._snapshot = snapshot
        self._modules.clear()
        return snapshot

    # --- Reconciliation in progress -------------------------------------------

    def _reconcile_state(self) -> ReconcileState | None:
        """The reconciliation being resolved, if there is one."""
        raw = self.store.read_pointer(RECONCILE_POINTER)
        # Cleared by writing nothing rather than by deleting, because ``BlockStore`` has no delete for a
        # pointer and adding one would change an interface third-party stores already implement.
        if not raw:
            return None
        return ReconcileState.model_validate(parse_json_strict(raw))

    def _put_reconcile_state(self, state: ReconcileState | None) -> None:
        """Record or clear the reconciliation in progress."""
        payload = b"" if state is None else canonicalize(state.model_dump(mode="json", exclude_none=True))
        self.store.write_pointer(RECONCILE_POINTER, payload)

    def _require_no_reconciliation(self, doing: str) -> None:
        """Refuse an operation that would write while a reconciliation is unresolved.

        The same rule version control applies mid-merge, and for the same reason: the state records decisions
        taken against a particular head, so a commit underneath it would leave those decisions describing a
        reconciliation that no longer exists. Every ordinary mutation funnels through one write path, so one
        guard covers all of them and none can be forgotten.
        """
        state = self._reconcile_state()
        if state is None:
            return
        raise ReconciliationHaltedError(
            f"cannot {doing}: the reconciliation of {state.theirs.short} is still unresolved. Resolve what is "
            f"open and continue it, or abandon it with reconcile_abort()."
        )

    @property
    def origin(self) -> Origin | None:
        """Where this brain was pulled from, if it was."""
        return self._state.origin if self._state else None

    def ancestry(self) -> list[OciDigest]:
        """
        The first-parent chain from the current snapshot back.

        This is the line the protocol reads as *what this brain is*: the first parent is the history a
        reconciliation was performed onto, and every rule that speaks of "the parent" means that one
        (paper Section 12.1). It is therefore the chain an audit follows to see how the brain got here,
        and the positions a signature's scope is judged against (Section 8.5).

        It is **not** what a containment check asks. A merged-in history is genuinely contained in this
        brain without appearing on this chain, so use :meth:`reachable_history` for that.

        Returns:
            list[OciDigest]: The current snapshot first, then each first parent still resolvable.
        """
        if self._state is None:
            return []
        chain = [self._state.snapshot]
        snapshot: Snapshot | None = self._snapshot
        while snapshot is not None and snapshot.first_parent is not None:
            parent = snapshot.first_parent
            chain.append(parent)
            if not self.store.is_resolvable(parent):
                break
            snapshot = Snapshot.from_document(self.store.get_bytes(parent))
        return chain

    def reachable_history(self) -> set[OciDigest]:
        """
        Every snapshot this brain's history contains, following all parents.

        A reconciliation names more than one parent, so history is a DAG rather than a chain, and
        "does this brain already contain that snapshot?" is a reachability question over the whole
        thing. That is what a fast-forward check asks: a push is safe when the remote's snapshot is in
        here, because then publishing drops nothing. Walking only :meth:`ancestry` would answer it
        wrongly in exactly the case reconciliation exists for -- after merging a contribution, the
        contributor's head is a parent of the local snapshot, and a push back to their repository would
        still be reported as divergence.

        Returns:
            set[OciDigest]: The current snapshot and every ancestor reachable through any parent.
            Traversal stops at snapshots the store cannot resolve, which are still reported: an
            ancestor that was pruned is part of the history even when its document is gone.
        """
        if self._state is None:
            return set()
        seen = {self._state.snapshot}
        frontier = [self._snapshot]
        while frontier:
            snapshot = frontier.pop()
            for parent in snapshot.parents:
                if parent in seen:
                    continue
                seen.add(parent)
                if self.store.is_resolvable(parent):
                    frontier.append(Snapshot.from_document(self.store.get_bytes(parent)))
        return seen

    # --- Discovery ------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """
        The current state of the brain.

        Returns:
            Snapshot: The installed modules and their roots.
        """
        return self._snapshot

    def root_of(self, memory_type: MemoryType) -> MerkleRoot:
        """
        The Merkle root of one installed module.

        Args:
            memory_type (MemoryType): Which module to look up.

        Returns:
            MerkleRoot: That module's root.

        Raises:
            SnapshotError: If the module is not installed.
        """
        return self._snapshot.root_of(memory_type)

    def module(self, memory_type: MemoryType) -> Module:
        """
        Open one installed module at the snapshot's version.

        The stored composition document is checked against the root the snapshot files it under, so a
        document that does not reproduce its root is refused rather than trusted.

        Args:
            memory_type (MemoryType): Which module to open.

        Returns:
            Module: That module.

        Raises:
            SnapshotError: If the module is not installed, or its composition does not match its root.
        """
        if memory_type in self._modules:
            return self._modules[memory_type]

        reference = self._snapshot.modules.get(memory_type)
        if reference is None:
            installed = ", ".join(sorted(kind.value for kind in self._snapshot.modules)) or "none"
            raise SnapshotError(f"the {memory_type.value} module is not installed; installed: {installed}")

        composition = Composition.from_document(self.store.get_bytes(reference.composition))
        if composition.root != reference.root:
            raise SnapshotError(
                f"the stored composition for {memory_type.value} has root {composition.root.short} but the "
                f"snapshot files it under {reference.root.short}"
            )
        module = Module(
            memory_type,
            self.store,
            composition,
            self._index_map(memory_type),
            tombstones=reference.tombstones or (),
        )
        self._modules[memory_type] = module
        return module

    def modules(self) -> dict[MemoryType, Module]:
        """
        Every installed module.

        Returns:
            dict[MemoryType, Module]: The modules, keyed by memory type.
        """
        return {kind: self.module(kind) for kind in self._snapshot.installed}

    def _index_map(self, memory_type: MemoryType) -> dict[str, Index]:
        return {index.kind.value: index for index in self.indices.get(memory_type, [])}

    def _module_or_empty(self, memory_type: MemoryType) -> Module:
        """The installed module, or an empty one, so the first write to a module works."""
        if self._snapshot.has_module(memory_type):
            return self.module(memory_type)
        return Module(memory_type, self.store, Composition(memory_type), self._index_map(memory_type))

    # --- Resolution and verification ------------------------------------------

    def resolve(self, block_id: BlockId) -> Block:
        """
        Resolve a ``block_id`` to its block, verifying its hash and its membership.

        A block that is in the store but in no installed composition was dropped, or belongs to a
        module this client did not install. Either way no installed root commits to it, so returning
        it would break the guarantee that every result is verified against the snapshot.

        Args:
            block_id (BlockId): The identity to resolve.

        Returns:
            Block: The decoded block.

        Raises:
            BlockNotFoundError: If no installed module holds it.
        """
        for memory_type in self._snapshot.installed:
            module = self.module(memory_type)
            if block_id in module:
                return module.get(block_id)
        raise BlockNotFoundError(f"block {block_id.short} is not in any installed composition of this snapshot")

    def prove(self, block_id: BlockId, memory_type: MemoryType) -> InclusionProof:
        """
        Prove that a block belongs to the installed snapshot.

        Args:
            block_id (BlockId): The block whose membership is proven.
            memory_type (MemoryType): Which module it belongs to.

        Returns:
            InclusionProof: A proof of size ``O(log n)``.
        """
        return self.module(memory_type).inclusion_proof(block_id)

    def verify(self) -> bool:
        """
        Verify every installed module end to end.

        Returns:
            bool: Whether every composition reproduces its recorded root and every resolvable block
            hashes to the identity it is filed under.
        """
        return all(self.module(kind).verify() for kind in self._snapshot.installed)

    # --- Authenticity -----------------------------------------------------------

    @property
    def trust_root(self) -> TrustRoot | None:
        """The keys authorized to sign for this brain, as the current snapshot carries them."""
        return self._snapshot.trust_root

    @property
    def trust_pin(self) -> TrustPin | None:
        """The anchor this consumer holds for this brain, or ``None`` before any was recorded."""
        return read_pin(self.store)

    def signatures(self, snapshot: OciDigest | None = None) -> list[SignatureRecord]:
        """
        The signature records held over a snapshot.

        Args:
            snapshot (OciDigest | None): Which snapshot. Defaults to the current one.

        Returns:
            list[SignatureRecord]: The records, possibly empty.
        """
        return for_snapshot(self.store, snapshot if snapshot is not None else self._snapshot.digest)

    def add_signature(self, record: SignatureRecord) -> OciDigest:
        """
        Keep a signature record someone else produced -- a countersignature, or one that arrived
        with a pull.

        Adding a signature never changes any snapshot's identity: that is the entire point of
        detaching them, and it is what lets a quorum accumulate across machines.

        Args:
            record (SignatureRecord): The record to keep.

        Returns:
            OciDigest: The record blob's content address.
        """
        return store_record(self.store, record)

    def authenticate(
        self,
        snapshot: OciDigest | None = None,
        policy: VerificationPolicy | None = None,
        stance: SnapshotStance = SnapshotStance.HEAD,
    ) -> AuthenticationReport:
        """
        Check who signed a snapshot, against the trust root in force at its position.

        The second of the two verifications, reported separately from the first: :meth:`verify`
        answers "is this brain intact" from the bytes alone, and this answers "who assembled it"
        against the key list the chain carries. A consumer MUST NOT collapse them -- "intact, and
        signed by an authorized key" and "intact, provenance unknown" are different facts.

        Args:
            snapshot (OciDigest | None): Which snapshot to authenticate. Defaults to the current
                one.
            policy (VerificationPolicy | None): Tolerances for this check. Defaults to the
                paper's defaults: one valid signature, no propose-scoped heads.
            stance (SnapshotStance): How the snapshot is being presented. ``HEAD`` -- the default --
                asks about a brain's current state, where a key the trust root does not list is an
                impersonation attempt. ``OFFERED`` asks about a proposal, where the same key is
                attributable instead: the author is named and nothing is authorized.

        Returns:
            AuthenticationReport: Every verdict and finding, with the four-state summary
            derived. Never raises for a protocol failure; call
            :meth:`AuthenticationReport.require_authorized` to turn the report into a typed
            refusal.

        Raises:
            SnapshotError: If ``snapshot`` names a document this store does not hold.
        """
        if snapshot is None or snapshot == self._snapshot.digest:
            document = self._snapshot
        else:
            if not self.store.is_resolvable(snapshot):
                raise SnapshotError(f"snapshot {snapshot.short} is not held, so it cannot be authenticated")
            document = Snapshot.from_document(self.store.get_bytes(snapshot))
        # Compromise markers come from the newest revision this brain knows -- the head's trust
        # root -- because a compromise is recorded after the positions it withdraws.
        return Authenticator(self.store, policy=policy).authenticate(
            document, current=self._snapshot.trust_root, stance=stance
        )

    def pin(self, trust_root: OciDigest | None = None, source: PinSource | None = None) -> TrustPin:
        """
        Record a trust root digest as the anchor for this brain.

        The one thing that comes from outside (paper Section 8.8). Pinning by default records
        the trust root the current snapshot carries -- trust on first use, the ``known_hosts``
        model -- and pinning an explicit digest records one compared out of band. Re-pinning
        overwrites: re-anchoring is the consumer's decision to make, deliberately.

        Args:
            trust_root (OciDigest | None): The digest to pin. Defaults to the digest of the
                trust root the current snapshot carries.
            source (PinSource | None): How this pin was established. Defaults to
                ``FIRST_USE`` when the digest was defaulted and ``OUT_OF_BAND`` when it was
                given explicitly, which is what actually happened in each case.

        Returns:
            TrustPin: The recorded pin.

        Raises:
            SnapshotError: If no digest was given and the current snapshot carries no trust
                root. There is nothing to anchor: a pin over nothing would satisfy nothing.
        """
        if trust_root is None:
            if self._snapshot.trust_root is None:
                raise SnapshotError(
                    "this brain carries no trust root, so there is nothing to pin; a brain acquires "
                    "one at init or through a trust-root revision"
                )
            anchored = self._snapshot.trust_root.digest
            defaulted = True
        else:
            anchored = trust_root
            defaulted = False
        genesis = None
        ancestry = self.ancestry()
        if ancestry:
            genesis = ancestry[-1]
        return write_pin(
            self.store,
            anchored,
            source if source is not None else (PinSource.FIRST_USE if defaulted else PinSource.OUT_OF_BAND),
            genesis=genesis,
            reference=self._state.origin.reference if self._state and self._state.origin else None,
        )

    def _snapshot_at(self, digest: OciDigest | None) -> Snapshot:
        """The snapshot document to operate on: the head, or a held historical one."""
        if digest is None or digest == self._snapshot.digest:
            return self._snapshot
        if not self.store.is_resolvable(digest):
            raise SnapshotError(f"snapshot {digest.short} is not held by this brain")
        return Snapshot.from_document(self.store.get_bytes(digest))

    def _authorship(self) -> Authorship:
        """The head's authorship line, recomputed only when the head or its records change."""
        index = read_index(self.store)
        key = (self._snapshot.digest.hex, tuple(index.entries.get(str(self._snapshot.digest), ())))
        cached = self._authorship_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        authorship = self.authenticate().authorship()
        self._authorship_cache = (key, authorship)
        return authorship

    def sign(
        self,
        signer: Signer,
        snapshot: OciDigest | None = None,
        scopes: Iterable[Scope] | None = None,
    ) -> SignatureRecord:
        """
        Produce a detached signature over a snapshot under the protocol namespace.

        Signing changes no identity anywhere: the record lands beside the snapshot, which is
        what lets a snapshot committed locally become signed at publication without changing
        digest, and what lets several signatures accumulate into a quorum.

        Args:
            signer (Signer): What produces the signature -- ordinarily an
                :class:`~boltzmann.authenticity.signers.AgentSigner`; the private key never
                enters this process.
            snapshot (OciDigest | None): Which snapshot to sign. Defaults to the current one.
            scopes (Iterable[Scope] | None): What the record claims. Defaults to the scope set
                the snapshot's difference actually required, so the claim matches what the
                snapshot did. A statement of intent that aids diagnosis, never the basis of any
                decision.

        Returns:
            SignatureRecord: The record, already persisted beside the snapshot.

        Raises:
            SnapshotError: If ``snapshot`` names a document this store does not hold.
            SignerUnavailableError: If the signing backend cannot sign.
        """
        document = self._snapshot_at(snapshot)
        if scopes is None:
            position = locate(self.store, document)
            claimed = tuple(sorted(required_scopes(gather_evidence(self.store, document, position.parent)).scopes))
        else:
            claimed = tuple(sorted(set(scopes)))
        signature = sshsig_sign(document.canonical_bytes(), signer)
        record = SignatureRecord(
            snapshot=document.digest,
            key=signer.public_key.fingerprint,
            scopes=claimed,
            signature=signature.armored(),
        )
        store_record(self.store, record)
        return record

    def plan_rotate(self, trust_root: TrustRoot) -> RotationPlan:
        """
        Build a trust-root revision without advancing the head.

        The multi-party half of governance. The revision document carries ``created_at``, so two
        independent constructions would produce different bytes and signatures over different
        digests -- it is therefore built **once**, here, and the exact bytes travel to each
        countersigner over any channel: nothing in them is secret, and each party inspects what
        it signs. When the records come back, :meth:`rotate` with ``plan=`` reuses these bytes.

        Args:
            trust_root (TrustRoot): The new key list.

        Returns:
            RotationPlan: The document, its digest, and who can satisfy the quorum.

        Raises:
            SnapshotError: If this brain carries no trust root -- authority is anchored at a
                genesis (:meth:`init`), never asserted onto an ungoverned chain, where no
                previous revision exists to draw a quorum from -- or if the new revision does
                not follow the one in force.
        """
        current = self._snapshot.trust_root
        if current is None:
            raise SnapshotError(
                "this brain carries no trust root to revise; authority is anchored at a genesis "
                "(Brain.init), never asserted onto an ungoverned chain"
            )
        revision = self._snapshot.with_trust_root(trust_root)
        return RotationPlan(
            document=revision.canonical_bytes(),
            digest=revision.digest,
            quorum_required=current.govern_quorum,
            eligible=tuple(entry.fingerprint for entry in current.govern_holders),
        )

    def countersign(self, document: bytes, signer: Signer) -> SignatureRecord:
        """
        Inspect a governance document someone else built, and sign its exact bytes.

        The counterparty's half of a multi-party rotation: the initiator's
        :meth:`plan_rotate` document arrives by any channel, and this is where reading it
        becomes approving it. The checks are mechanical -- the same ones a verifier will later
        run -- so what is signed is exactly what was claimed: a pure change of authority over a
        history this brain already holds.

        Args:
            document (bytes): The revision snapshot's canonical bytes, verbatim as received. The
                signature covers these bytes; any re-serialization would sign something else.
            signer (Signer): What produces the signature.

        Returns:
            SignatureRecord: The record to send back to the initiator, also kept locally.

        Raises:
            SnapshotError: If the bytes are not a canonical snapshot document.
            ResolutionRefusedError: If the document is not a pure revision over a history this
                brain holds -- content smuggled into a governance act, an unknown parent, a
                removed or non-advancing trust root, an admission claim the observable chain
                refutes, or a parentless genesis this brain does not hold. Refusal names what
                failed; nothing is signed.
        """
        revision = Snapshot.from_document(document)
        if revision.trust_root is None:
            raise ResolutionRefusedError(
                "the document carries no trust root; countersigning is for governance acts, and "
                "removing authority outright is not one this operation will endorse"
            )
        parent_digest = revision.first_parent
        if parent_digest is not None:
            if not self.store.is_resolvable(parent_digest):
                raise ResolutionRefusedError(
                    f"the document's parent {parent_digest.short} is not held here; fetch the history "
                    f"first -- a countersignature over an unverifiable transition is exactly what this "
                    f"check exists to prevent"
                )
            if parent_digest != self._snapshot.digest and parent_digest not in self.reachable_history():
                raise ResolutionRefusedError(
                    f"the document's parent {parent_digest.short} is not in this brain's history; a "
                    f"countersigner endorses a transition of a history it can see"
                )
            parent = Snapshot.from_document(self.store.get_bytes(parent_digest))
            if revision.modules != parent.modules:
                raise ResolutionRefusedError(
                    "the document changes module roots as well as the trust root; a revision changes "
                    "the key list and nothing else, and content smuggled into a governance act is the "
                    "attack this check exists for"
                )
            if parent.trust_root is not None and revision.trust_root.revision <= parent.trust_root.revision:
                raise ResolutionRefusedError(
                    f"the document's revision {revision.trust_root.revision} does not follow the "
                    f"revision {parent.trust_root.revision} in force at its parent"
                )
        elif revision.digest != self._snapshot.digest and revision.digest not in self.reachable_history():
            raise ResolutionRefusedError(
                f"the document is a parentless genesis ({revision.digest.short}) this brain does "
                f"not hold; a genesis asserts authority from nothing, so countersigning one is "
                f"only offered for a genesis already in this brain's own history -- pull and "
                f"inspect the brain first, then countersign what you actually hold"
            )
        observed = observed_revisions(self.store, revision)
        for entry in revision.trust_root.keys:
            if confirm_since(observed, entry) is SinceVerdict.REFUTED:
                raise ResolutionRefusedError(
                    f"key {entry.fingerprint} claims authorization since revision {entry.since}, "
                    f"which the observable chain refutes; this document lies about its own history"
                )
        signature = sshsig_sign(document, signer)
        record = SignatureRecord(
            snapshot=revision.digest,
            key=signer.public_key.fingerprint,
            scopes=(Scope.GOVERN,),
            signature=signature.armored(),
        )
        store_record(self.store, record)
        return record

    def rotate(
        self,
        trust_root: TrustRoot | None = None,
        signers: Sequence[Signer] = (),
        records: Sequence[SignatureRecord] = (),
        plan: RotationPlan | None = None,
    ) -> RotationResult:
        """
        Commit a trust-root revision, under the quorum rule.

        Blobs first, pointer last, quorum in between: the revision document and every signature
        are written, the quorum is evaluated against the trust root as it stood **before** the
        change -- the half of the rule that is easy to lose -- and only then does the head move.
        A failed quorum advances nothing.

        Single owner: ``rotate(new_root, signers=[agent])`` in one call. Quorum of two or more
        across machines: build once with :meth:`plan_rotate`, collect
        :meth:`countersign` records over the planned bytes, then ``rotate(plan=...,
        records=...)``.

        Args:
            trust_root (TrustRoot | None): The new key list, when building fresh. Exactly one of
                this or ``plan`` is given.
            signers (Sequence[Signer]): Local signers to sign with now.
            records (Sequence[SignatureRecord]): Signatures collected elsewhere. Each must cover
                the exact revision being committed -- which is why multi-party flows pass
                ``plan``: a rebuilt document has a different ``created_at`` and a different
                digest, and a record over the planned bytes can never match it.
            plan (RotationPlan | None): The planned document, when signatures were collected
                over :meth:`plan_rotate` output.

        Returns:
            RotationResult: What took effect, and on whose signatures.

        Raises:
            SnapshotError: If neither or both of ``trust_root`` and ``plan`` were given, the
                brain carries no trust root, the planned document does not extend this head, or
                a provided record covers something else.
            QuorumFailureError: If fewer than ``govern_quorum`` distinct keys holding ``govern``
                in the previous revision validly signed. Nothing is advanced.
        """
        self._require_no_reconciliation("rotate the trust root of this brain")
        previous = self._snapshot.trust_root
        if previous is None:
            raise SnapshotError(
                "this brain carries no trust root to revise; authority is anchored at a genesis "
                "(Brain.init), never asserted onto an ungoverned chain"
            )
        if (trust_root is None) == (plan is None):
            raise SnapshotError("exactly one of trust_root (build fresh) or plan (reuse planned bytes) is given")
        if plan is not None:
            revision = Snapshot.from_document(plan.document)
            if revision.digest != plan.digest:
                raise SnapshotError("the plan's document and digest disagree; it was altered in transit")
            if revision.first_parent != self._snapshot.digest:
                raise SnapshotError(
                    f"the plan extends {revision.first_parent}, and this brain's head is now "
                    f"{self._snapshot.digest.short}; the head moved since planning, so plan again"
                )
            if revision.trust_root is None:
                raise SnapshotError("the planned document carries no trust root, so it revises nothing")
        else:
            assert trust_root is not None
            revision = self._snapshot.with_trust_root(trust_root)
        digest = revision.digest
        message = revision.canonical_bytes()

        collected: list[SignatureRecord] = []
        seen: set[str] = set()
        for record in records:
            if record.snapshot != digest:
                raise SnapshotError(
                    f"a provided record covers {record.snapshot.short}, not this revision "
                    f"{digest.short}; when signatures were collected over a planned document, pass "
                    f"plan= so the exact bytes are reused -- a rebuilt document has a different "
                    f"created_at and a different digest"
                )
            if record.digest.hex not in seen:
                seen.add(record.digest.hex)
                collected.append(record)
        for signer in signers:
            signature = sshsig_sign(message, signer)
            record = SignatureRecord(
                snapshot=digest,
                key=signer.public_key.fingerprint,
                scopes=(Scope.GOVERN,),
                signature=signature.armored(),
            )
            if record.digest.hex not in seen:
                seen.add(record.digest.hex)
                collected.append(record)

        met = Authenticator(self.store).quorum_count(locate(self.store, revision), collected)
        if met < previous.govern_quorum:
            raise QuorumFailureError(
                f"a trust-root revision requires {previous.govern_quorum} valid signature(s) from "
                f"distinct keys holding govern in revision {previous.revision}; {met} qualified. "
                f"Nothing was advanced: collect the missing countersignatures over the planned bytes "
                f"and rotate again"
            )
        for record in collected:
            store_record(self.store, record)
        self._advance(revision)
        assert revision.trust_root is not None
        self._warn_without_governance_margin(revision.trust_root, self._snapshot.digest.short)
        return RotationResult(
            snapshot=digest,
            revision=revision.trust_root.revision,
            quorum_required=previous.govern_quorum,
            quorum_met=met,
            records=tuple(collected),
        )

    def revoke(
        self,
        key: SshPublicKey | str,
        signers: Sequence[Signer] = (),
        records: Sequence[SignatureRecord] = (),
        retired_from: int | None = None,
        compromised_from: OciDigest | None = None,
    ) -> RotationResult:
        """
        Record that a key is retired, or compromised from a chain position.

        The two look similar and behave oppositely (paper Section 8.6). Retirement closes an
        interval without disturbing what came before: everything the key signed while authorized
        stays valid, which is what makes an ordinary departure harmless. A compromise withdraws
        everything from the recorded position onward, even though it was signed while the key
        was listed -- the only construct in the protocol that invalidates a previously valid
        signature.

        A revocation is a trust-root revision and needs the same quorum; this method builds the
        revised key list and delegates to :meth:`rotate`. For a quorum spanning machines, build
        the revised root, :meth:`plan_rotate` it, and collect countersignatures instead.

        Args:
            key (SshPublicKey | str): The key -- an :class:`SshPublicKey`, a ``SHA256:``
                fingerprint, or an authorized_keys line.
            signers (Sequence[Signer]): Who signs the revision.
            records (Sequence[SignatureRecord]): Signatures collected elsewhere.
            retired_from (int | None): The revision from which the key stops being authorized.
                Defaults, when no compromise is recorded, to the revision this call creates --
                "retired as of now", the ordinary departure.
            compromised_from (OciDigest | None): The snapshot from which the key's signatures
                are withdrawn. Mid-span positions are the point: compromise is discovered after
                the fact, so this need not fall on any revision boundary.

        Returns:
            RotationResult: The revision that recorded it.

        Raises:
            ValueError: If both positions are given. They express opposite intents, and a call
                that means both means nothing.
            SnapshotError: If the brain carries no trust root, or the key is not listed in it.
            QuorumFailureError: If the quorum is not met. Nothing is advanced.
        """
        if retired_from is not None and compromised_from is not None:
            raise ValueError(
                "retired_from and compromised_from express opposite intents -- one preserves the "
                "key's history and the other withdraws it -- and a call that means both means nothing"
            )
        current = self._snapshot.trust_root
        if current is None:
            raise SnapshotError("this brain carries no trust root, so there is no authority to revoke a key from")
        wanted: SshPublicKey | None = None
        if isinstance(key, SshPublicKey) or not key.startswith("SHA256:"):
            wanted = SshPublicKey.parse(key)
        entry: TrustedKey | None = None
        for candidate in current.keys:
            if (wanted is not None and candidate.key.matches(wanted)) or (
                wanted is None and candidate.fingerprint == key
            ):
                entry = candidate
        if entry is None:
            raise SnapshotError(f"key {key!r} is not listed in the trust root in force, so there is nothing to revoke")
        next_revision = current.revision + 1
        if compromised_from is not None:
            replacement = entry.model_copy(update={"compromised_from": compromised_from})
        else:
            if entry.retired_from is not None:
                raise SnapshotError(
                    f"key {entry.fingerprint} is already retired from revision {entry.retired_from}; "
                    f"a second retirement would record nothing"
                )
            replacement = entry.model_copy(update={"retired_from": retired_from or next_revision})
        revised = current.model_copy(
            update={
                "revision": next_revision,
                "keys": tuple(replacement if candidate is entry else candidate for candidate in current.keys),
            }
        )
        return self.rotate(trust_root=TrustRoot.model_validate(revised.model_dump()), signers=signers, records=records)

    def resolvability(self) -> ResolvabilityReport:
        """
        Report what resolves, what was tombstoned, and what is simply missing.

        The three-way split is required, not cosmetic: a redacted block and a corrupted one both fail to
        read, and a consumer that cannot tell them apart cannot tell a lawful erasure from a broken
        store (paper Section 10.6).

        The same split is reported for the content a block names but does not carry. Such a block can be
        whole and its composition consistent while the datum it names is gone, and no other reader would
        say so: :meth:`verify` tolerates absent bytes by design, and a ``prune`` finds nothing to reclaim
        because a retained root still names them. Without this the store looks intact until the module is
        packed for publication, which is the worst place to learn it.

        This reads no content. Classifying it asks the store which digests it holds, exactly as the block
        half does, so the cost stays a pass over envelopes.

        Returns:
            ResolvabilityReport: The classification, per module.
        """
        resolvable: dict[MemoryType, list[BlockId]] = {}
        tombstoned: dict[MemoryType, list[BlockId]] = {}
        missing: dict[MemoryType, list[BlockId]] = {}
        content_resolvable: dict[MemoryType, list[Digest]] = {}
        content_tombstoned: dict[MemoryType, list[Digest]] = {}
        content_missing: dict[MemoryType, list[Digest]] = {}

        for memory_type in self._snapshot.installed:
            module = self.module(memory_type)
            classified: set[str] = set()

            for block_id in module.block_ids:
                if block_id in module.tombstones or not self.store.is_resolvable(block_id):
                    unreadable = tombstoned if block_id in module.tombstones or self.store.has(block_id) else missing
                    unreadable.setdefault(memory_type, []).append(block_id)
                    continue

                resolvable.setdefault(memory_type, []).append(block_id)

                # Only a readable block can say what it names, which is why this lives here rather
                # than in a second pass. Two blocks may name the same datum; it is one datum.
                for digest in module.get(block_id).content_digests:
                    if digest.hex in classified:
                        continue
                    classified.add(digest.hex)
                    if self.store.is_resolvable(digest):
                        content_resolvable.setdefault(memory_type, []).append(digest)
                    elif self.store.has(digest):
                        content_tombstoned.setdefault(memory_type, []).append(digest)
                    else:
                        content_missing.setdefault(memory_type, []).append(digest)

        return ResolvabilityReport(
            resolvable=resolvable,
            tombstoned=tombstoned,
            missing=missing,
            content_resolvable=content_resolvable,
            content_tombstoned=content_tombstoned,
            content_missing=content_missing,
        )

    def audit_validation(self) -> ValidationAudit:
        """
        Report which committed blocks can show the verdict that admitted them.

        Every block entering a composition must be accompanied by a validation record in provenance
        naming the verdict, the checks that ran, and the task (paper Section 10.3). This reads the
        ledger back and says where that record is missing.

        It reports rather than refuses. A brain written before the record existed is not a brain that
        did anything wrong, and refusing it would take availability away to gain an auditability the
        snapshot cannot retroactively supply. The invariant that *does* refuse is the removal one,
        because there a missing record is how the ledger would be quietly emptied.

        Returns:
            ValidationAudit: The derived blocks whose verdict is readable, and those whose is not.
        """
        ledger = Ledger.of(self.modules())
        accounted: dict[MemoryType, list[BlockId]] = {}
        unaccounted: dict[MemoryType, list[BlockId]] = {}

        for memory_type in self._snapshot.installed:
            if memory_type in (MemoryType.CANONICAL, MemoryType.PROVENANCE):
                continue
            for block_id in self.module(memory_type).block_ids:
                bucket = accounted if block_id in ledger.validations else unaccounted
                bucket.setdefault(memory_type, []).append(block_id)

        return ValidationAudit(accounted=accounted, unaccounted=unaccounted)

    def open_index(self, memory_type: MemoryType, kind: IndexKind) -> Index:
        """
        Open one of a module's indices.

        Opening an index is not the same as querying through it: a query never names an index
        (Principle 7). This exists for tooling that inspects or rebuilds a brain.

        Args:
            memory_type (MemoryType): Which module's index to open.
            kind (IndexKind): Which index.

        Returns:
            Index: The opened index.

        Raises:
            QueryError: If no index of that kind was registered for the module. The SDK ships no index
                engine, so an index exists only if the caller supplied it.
        """
        for index in self.indices.get(memory_type, []):
            if index.kind is kind:
                return index
        registered = ", ".join(index.kind.value for index in self.indices.get(memory_type, [])) or "none"
        raise QueryError(
            f"no {kind.value} index is registered for the {memory_type.value} module; registered: "
            f"{registered}. The SDK ships no index engine, so pass one to Brain(indices=...)."
        )

    # --- Query ----------------------------------------------------------------

    def search(self, query: Query) -> EvidenceBundle:
        """
        Retrieve verified evidence for a declarative query.

        With a :class:`~boltzmann.query.planner.QueryPlanner` injected, candidate generation and ranking
        are the planner's. Without one, the built-in scan does the part that belongs to the protocol --
        filter, resolve, verify, report provenance -- by linear traversal, with a score that is term
        coverage rather than relevance. Either way the result is data with its provenance, never prose,
        and every match is verified against the installed snapshot.

        Args:
            query (Query): The declarative request. It names no index.

        Returns:
            EvidenceBundle: Verified matches.
        """
        modules = self.modules()
        bundle = self.planner.plan(query, modules) if self.planner is not None else scan(query, modules)
        # The second verification rides along with the first: ``verified`` covers hashes and
        # membership, ``authorship`` covers who assembled the brain, and the two are never folded
        # (paper Section 9.3). Cached because a query must not pay for a chain walk the previous
        # query already paid for -- and invalidated by anything that could change the answer,
        # which is the head or the record set.
        return bundle.model_copy(update={"authorship": self._authorship()})

    # --- Catalog --------------------------------------------------------------

    def classify(
        self,
        request: ClassificationRequest | Sequence[CatalogDeclaration],
    ) -> ClassificationResult:
        """Declare catalog structure or place canonical sources in catalog classes.

        Declarations are checked sequentially, so one atomic request may declare a scheme, its
        classes, their hierarchy, and placements that refer to those new classes. Invalid declarations
        receive verdicts and are omitted; every validated declaration is committed in one snapshot.

        Args:
            request (ClassificationRequest | Sequence[CatalogDeclaration]): Catalog declarations.

        Returns:
            ClassificationResult: One verdict per declaration and the commit they produced.
        """
        typed = (
            request if isinstance(request, ClassificationRequest) else ClassificationRequest(declarations=list(request))
        )
        verdicts, blocks, placements = validate_declarations(typed, self.modules())
        if not blocks:
            return ClassificationResult(verdicts=verdicts, commit=CommitResult(snapshot=self._snapshot))

        now = utc_timestamp()
        producer = Producer(kind=ProducerKind.ACTOR, id=self.actor.id)
        provenance = [
            ProvenanceBlock(
                record=DerivationRecord(
                    block=placement.block_id,
                    derived_from=[placement.source],
                    producer=producer,
                    actor=self.actor,
                    at=now,
                    task="catalog-placement",
                )
            )
            for placement in placements
        ]
        commit = self._write(blocks={MemoryType.SEMANTIC: list(blocks)}, provenance=provenance)
        return ClassificationResult(verdicts=verdicts, commit=commit)

    def browse(self, classes: BlockId | Sequence[BlockId]) -> CatalogBrowseResult:
        """Browse canonical sources classified in one class or a faceted intersection."""
        return Catalog(self.modules()).browse(classes)

    def catalog_path(self, schemes: Sequence[str]) -> CatalogPathView:
        """Build a virtual slash-separated view using the given scheme order."""
        return CatalogPathView(self, schemes)

    # --- Ingestion: register --------------------------------------------------

    def register(self, data: bytes, request: RegistrationRequest) -> RegistrationResult:
        """
        Preserve a source as canonical evidence.

        Registering a source does not declare it true: the canonical module asserts that the evidence
        was incorporated and preserved (paper Section 8.1).

        Re-registering identical bytes is a genuine no-op. The canonical block is a statement about
        the bytes and nothing else, so a second registration computes the same identity, adds no
        block, and publishes no snapshot.

        Args:
            data (bytes): The original bytes, exactly as observed.
            request (RegistrationRequest): Who is registering what, and under what policy.

        Returns:
            RegistrationResult: The canonical block's identity, and the commit if one happened.

        Raises:
            ProtocolError: If a normalization pipeline is named that does not accept this media type.
        """
        blob = self.store.put_bytes(data)
        view, normalization = self._normalize(data, request)

        block = CanonicalBlock(
            blob=blob,
            media_type=request.media_type,
            size=len(data),
            normalized_view=view,
        )

        canonical = self._module_or_empty(MemoryType.CANONICAL)
        if block.block_id in canonical:
            return RegistrationResult(block_id=block.block_id, duplicate=True)

        registration = RegistrationRecord(
            block=block.block_id,
            actor=request.actor,
            at=utc_timestamp(),
            origin=request.origin,
            license=request.license,
            retention_policy=request.retention_policy,
        )
        records: list[ProvenanceBlock] = [ProvenanceBlock(record=registration)]
        if normalization is not None:
            records.append(ProvenanceBlock(record=normalization.model_copy(update={"block": block.block_id})))

        commit = self._write(
            blocks={MemoryType.CANONICAL: [block]},
            provenance=records,
        )
        return RegistrationResult(block_id=block.block_id, commit=commit)

    def replace(self, data: bytes, request: RegistrationRequest, supersedes: BlockId) -> RegistrationResult:
        """
        Register a newer edition of a source and record that it takes precedence.

        Register plus a supersession edge, never a mutation of bytes already stored. The superseded
        original stays in the composition for audit; dropping it is a separate, explicit decision.

        Args:
            data (bytes): The new original's bytes.
            request (RegistrationRequest): Who is registering what, and under what policy.
            supersedes (BlockId): The canonical block the new edition replaces.

        Returns:
            RegistrationResult: The new canonical block's identity and the resulting commit.

        Raises:
            ProtocolError: If the superseded block is not in the canonical composition.
        """
        canonical = self._module_or_empty(MemoryType.CANONICAL)
        if supersedes not in canonical:
            raise ProtocolError(
                f"cannot supersede {supersedes.short}: it is not in the canonical composition, so there is "
                f"nothing for the new edition to take precedence over"
            )

        blob = self.store.put_bytes(data)
        view, normalization = self._normalize(data, request)
        block = CanonicalBlock(blob=blob, media_type=request.media_type, size=len(data), normalized_view=view)

        if block.block_id == supersedes:
            raise ProtocolError(
                "the new edition is byte-identical to the one it would supersede, so there is no new "
                "evidence to register"
            )

        now = utc_timestamp()
        records: list[ProvenanceBlock] = [
            ProvenanceBlock(
                record=RegistrationRecord(
                    block=block.block_id,
                    actor=request.actor,
                    at=now,
                    origin=request.origin,
                    license=request.license,
                    retention_policy=request.retention_policy,
                )
            ),
            ProvenanceBlock(
                record=SupersessionRecord(
                    block=block.block_id,
                    supersedes=supersedes,
                    actor=request.actor,
                    at=now,
                )
            ),
        ]
        if normalization is not None:
            records.append(ProvenanceBlock(record=normalization.model_copy(update={"block": block.block_id})))

        already_present = block.block_id in canonical
        commit = self._write(
            blocks={} if already_present else {MemoryType.CANONICAL: [block]},
            provenance=records,
        )
        return RegistrationResult(block_id=block.block_id, commit=commit, duplicate=already_present)

    def _normalize(
        self, data: bytes, request: RegistrationRequest
    ) -> tuple[NormalizedView | None, NormalizationRecord | None]:
        """Run the named deterministic pipeline, if one was asked for."""
        if request.normalize_with is None:
            return None, None

        pipeline = get_pipeline(request.normalize_with)
        if not pipeline.accepts(request.media_type):
            raise ProtocolError(
                f"pipeline {pipeline.name!r} does not accept {request.media_type!r}, so it cannot produce a "
                f"normalized view of this source"
            )
        normalized = pipeline.normalize(data)
        view = NormalizedView(
            blob=self.store.put_bytes(normalized),
            media_type=pipeline.output_media_type,
            size=len(normalized),
        )
        record = NormalizationRecord(
            block=BlockId.of(b""),  # replaced with the canonical block's identity by the caller
            pipeline=pipeline.name,
            pipeline_version=pipeline.version,
            actor=request.actor,
            at=utc_timestamp(),
        )
        return view, record

    def put_content(self, data: bytes, media_type: str) -> ContentRef:
        """
        Materialize bytes a block will name rather than carry.

        A payload is JSON, canonically serialized and hashed on every access, so a datum large enough to
        matter belongs in the store with the block naming it. This stores the bytes and returns the
        reference to put in a payload.

        It writes no block, touches no composition and publishes no snapshot -- there is nothing to
        commit yet. The bytes become reachable only once a committed block names them, and until then a
        ``prune`` will reclaim them, which is the correct outcome for content nothing refers to.

        **This is not registration.** Evidence goes through :meth:`register`: it lands in the canonical
        composition, other blocks cite it, and dropping it cascades to everything derived from it.
        Content is the block's own datum, so nothing cites it and nothing needs to; it lives and dies
        with its block. If other blocks are going to cite these bytes, they are a source, and the call
        is :meth:`register`.

        **The reference this returns is a fact, not a claim.** ``size`` is measured from the bytes
        rather than accepted from the caller, and ``media_type`` has to be shaped like one. Both end up
        in a payload, hashed into a ``block_id`` and published, and a consumer reads them to decide
        whether to fetch the content at all -- so a wrong value is not correctable afterwards, only
        replaceable by a different block. This is the one point where the bytes are in hand, which
        makes it the only place the declaration can be checked for free.

        Args:
            data (bytes): The content, stored exactly as given.
            media_type (str): IANA media type as ``type/subtype``, recorded in the reference so a
                consumer can decide whether to fetch the bytes without holding them.

        Returns:
            ContentRef: The reference a payload names, with ``size`` measured from ``data``.

        Raises:
            ProtocolError: If ``media_type`` is not of the form ``type/subtype``.
        """
        require_media_type(media_type)
        return ContentRef(blob=self.store.put_bytes(data), media_type=media_type, size=len(data))

    # --- Ingestion: delegate, validate, commit --------------------------------

    def define_task(
        self,
        source: BlockId,
        allowed: Iterable[MemoryType] | None = None,
        requirements: Sequence[str] | None = None,
        instructions: str | None = None,
        task_id: str | None = None,
    ) -> ProcessingTask:
        """
        Define a processing task and output schema for an external LLM.

        Args:
            source (BlockId): The canonical block to interpret. Must be installed, or the model would
                be asked to interpret evidence the brain does not hold.
            allowed (Iterable[MemoryType] | None): Which kinds of block may be proposed. Defaults to
                episodic, semantic, and procedural; canonical and provenance are never proposable.
            requirements (Sequence[str] | None): Constraints the proposal must respect.
            instructions (str | None): Free-form guidance.
            task_id (str | None): Identifier the resulting provenance records cite.

        Returns:
            ProcessingTask: The task to hand to a proposer.

        Raises:
            ProtocolError: If ``source`` is not in the canonical composition.
        """
        canonical = self._module_or_empty(MemoryType.CANONICAL)
        if source not in canonical:
            raise ProtocolError(f"cannot define a task over {source.short}: it is not in the canonical composition")
        return ProcessingTask(
            operation=TaskOperation.EXTRACT_KNOWLEDGE,
            source=source,
            allowed_memory_types=sorted(allowed or PROPOSABLE_MEMORY_TYPES),
            requirements=list(requirements or ["cite source ranges", "do not invent"]),
            instructions=instructions,
            task_id=task_id,
        )

    def candidates_schema(self, task: ProcessingTask) -> dict[str, Any]:
        """
        The JSON Schema a proposer's answer to this task must satisfy.

        ``task.output_schema`` names the schema; this returns it. Handing it to a model as structured
        output is what turns "propose typed blocks" from a hope into a constraint: the payload is
        resolved per memory type and narrowed to the types this task allows, so the model cannot even
        express a proposal the validation gate would reject on shape.

        Args:
            task (ProcessingTask): The task the schema should describe.

        Returns:
            dict[str, Any]: A self-contained JSON Schema, generated from the same block classes the gate
            validates against, so the two cannot disagree.
        """
        return _candidates_schema(task)

    def define_rederivation(
        self,
        source: BlockId,
        replacing: BlockId,
        allowed: Iterable[MemoryType] | None = None,
        task_id: str | None = None,
    ) -> ProcessingTask:
        """
        Define a task that regenerates knowledge against a replacement source.

        Re-derivation is never implicit. Section 8.1 is explicit that it runs only when the caller has
        registered a replacement canonical or asks for one, because a block's citation is part of its
        identity: one citing excluded evidence cannot be repaired in place, only replaced by a new block
        citing the new source. So this is a distinct operation rather than a flag on a drop.

        Args:
            source (BlockId): The replacement canonical block to derive from.
            replacing (BlockId): The canonical block whose derived knowledge is being regenerated. Named
                in the task so the resulting provenance says what this run was replacing.
            allowed (Iterable[MemoryType] | None): Which kinds of block may be proposed.
            task_id (str | None): Identifier the resulting provenance records cite.

        Returns:
            ProcessingTask: A ``rederive`` task over the replacement source.

        Raises:
            ProtocolError: If the replacement is not installed, or is the block it would replace.
        """
        if source == replacing:
            raise ProtocolError(f"cannot re-derive {source.short} against itself")
        canonical = self._module_or_empty(MemoryType.CANONICAL)
        if source not in canonical:
            raise ProtocolError(
                f"cannot re-derive against {source.short}: it is not in the canonical composition, so "
                f"there is no new evidence to derive from"
            )
        return ProcessingTask(
            operation=TaskOperation.REDERIVE,
            source=source,
            allowed_memory_types=sorted(allowed or PROPOSABLE_MEMORY_TYPES),
            requirements=[
                "cite source ranges",
                "do not invent",
                f"regenerate what was derived from {replacing}",
            ],
            instructions=f"The evidence {replacing} was excluded. Derive the equivalent knowledge from {source}.",
            task_id=task_id,
        )

    def validate(self, candidates: CandidateSet, task: ProcessingTask) -> ValidationReport:
        """
        Run the validation gate over what an external model proposed.

        Args:
            candidates (CandidateSet): The proposals.
            task (ProcessingTask): The task they answer.

        Returns:
            ValidationReport: One verdict per proposal. Nothing is stored yet.
        """
        return validate(candidates, task, self.modules(), self.validators)

    def commit(self, report: ValidationReport) -> CommitResult:
        """
        Incorporate every validated candidate, atomically.

        For each accepted block: serialize canonically, hash, store immutably, connect it to its
        source via provenance, incorporate it into the right Merkle DAG, update the indices, and
        publish a new snapshot (paper Section 8.3). Candidates that were rejected or contradicted are
        ignored, not retried.

        Args:
            report (ValidationReport): The verdicts to act on.

        Returns:
            CommitResult: The new snapshot, the blocks written, and the new roots. Empty if nothing
            was committable, in which case no snapshot is published.
        """
        committable = report.committable
        if not committable:
            return CommitResult(snapshot=self._snapshot)

        producer = report.producer or Producer(kind=ProducerKind.ACTOR, id=self.actor.id)
        now = utc_timestamp()
        # A report from before the gate carried its check set would otherwise write a record naming no
        # check, which the schema refuses -- correctly, since a verdict under an unstated check set is
        # not a claim anyone can act on.
        checks = list(report.checks) or [_UNRECORDED_CHECKS]

        blocks: dict[MemoryType, list[Block]] = {}
        provenance: list[ProvenanceBlock] = []
        for result in committable:
            block = result.block
            assert block is not None  # is_committable guarantees it
            blocks.setdefault(block.MEMORY_TYPE, []).append(block)
            provenance.append(
                ProvenanceBlock(
                    record=DerivationRecord(
                        block=block.block_id,
                        derived_from=list(result.candidate.evidence),
                        producer=producer,
                        actor=self.actor,
                        at=now,
                        task=report.task_id,
                        locator=result.candidate.locator,
                    )
                )
            )
            # The verdict travels with the block rather than staying on the write path. A consumer that
            # meets this composition can then read what admitted each member instead of trusting whoever
            # committed it, which is the whole difference between a ledger and a habit.
            provenance.append(
                ProvenanceBlock(
                    record=ValidationRecord(
                        block=block.block_id,
                        verdict=result.status,
                        checks=checks,
                        actor=self.actor,
                        at=now,
                        task=report.task_id,
                    )
                )
            )

        return self._write(blocks=blocks, provenance=provenance)

    def ingest(
        self,
        data: bytes,
        request: RegistrationRequest,
        proposer: CandidateProposer,
        allowed: Iterable[MemoryType] | None = None,
        use_normalized_view: bool = True,
    ) -> CommitResult:
        """
        Run the whole ingestion path: register, delegate, validate, commit.

        The four operations in the order Section 11 lays out. The proposer is supplied by the caller,
        because the protocol embeds no model: what knowledge a source yields is the external model's
        judgment, and what gets stored is the protocol's.

        Args:
            data (bytes): The original bytes.
            request (RegistrationRequest): Who is registering what.
            proposer (CandidateProposer): The external model's adapter.
            allowed (Iterable[MemoryType] | None): Which kinds of block may be proposed.
            use_normalized_view (bool): Whether to hand the proposer the normalized view rather than
                the original, when one was produced. Normalized views exist to be read.

        Returns:
            CommitResult: What was committed. Empty if the source was already registered and the model
            proposed nothing new.
        """
        registration = self.register(data, request)
        task = self.define_task(registration.block_id, allowed=allowed)

        source = data
        if use_normalized_view:
            block = self.module(MemoryType.CANONICAL).get(registration.block_id)
            view = getattr(block, "normalized_view", None)
            if view is not None:
                source = self.store.get_bytes(view.blob)

        return self.commit(self.validate(proposer(task, source), task))

    # --- The single write path -------------------------------------------------

    def _write(
        self,
        blocks: dict[MemoryType, list[Block]],
        provenance: Sequence[ProvenanceBlock],
        without: dict[MemoryType, list[BlockId]] | None = None,
        tombstones: Mapping[MemoryType, Iterable[BlockId]] | None = None,
    ) -> CommitResult:
        """
        Store blocks, advance the affected compositions, and publish a snapshot.

        Every mutation in this class funnels through here, which is what makes "the LLM never writes
        directly to the Merkle DAGs or to the indices" a property of the code: there is one place that
        writes, and it is reached only after validation.

        Adding and removing go through the same path, so a drop is one version just as a commit is: the
        blocks it excludes and the removal record it writes land in a single snapshot rather than two.

        Args:
            blocks (dict[MemoryType, list[Block]]): Blocks to add, by module.
            provenance (Sequence[ProvenanceBlock]): Provenance entries to record alongside them.
            without (dict[MemoryType, list[BlockId]] | None): Blocks to exclude from a composition. The
                blocks themselves are untouched -- what changes is which composition names them.
            tombstones (Mapping[MemoryType, Iterable[BlockId]] | None): Destroyed identities to add
                to module references without changing their compositions or Merkle roots.

        Returns:
            CommitResult: The new snapshot and the new roots.
        """
        self._require_no_reconciliation("write to this brain")

        by_module: dict[MemoryType, list[Block]] = {kind: list(items) for kind, items in blocks.items()}
        if provenance:
            by_module.setdefault(MemoryType.PROVENANCE, []).extend(provenance)

        excluded = {kind: list(ids) for kind, ids in (without or {}).items()}
        destroyed = {kind: list(ids) for kind, ids in (tombstones or {}).items()}
        touched = [
            *by_module,
            *(kind for kind in excluded if kind not in by_module),
            *(kind for kind in destroyed if kind not in by_module and kind not in excluded),
        ]

        committed: list[BlockId] = []
        references: list[ModuleRef] = []
        roots: dict[MemoryType, MerkleRoot] = {}

        for memory_type in touched:
            items = by_module.get(memory_type, [])
            for block in items:
                self.store.put_block(block)

            module = self._module_or_empty(memory_type).with_blocks(block.block_id for block in items)
            if memory_type in excluded:
                module = module.without_blocks(excluded[memory_type])
            if memory_type in destroyed:
                module = module.with_tombstones(destroyed[memory_type])
            self._rebuild_indices(module)
            embedding_model, index_digest = self._travelling_index_binding(memory_type)
            reference = module.persist(embedding_model=embedding_model, index_digest=index_digest)
            references.append(reference)
            roots[memory_type] = reference.root
            if memory_type is not MemoryType.PROVENANCE:
                committed.extend(block.block_id for block in items)

        # One commit is one version, however many modules it advanced. A brain's first version has no
        # parent: the empty snapshot a fresh handle starts from is a placeholder, never a published
        # document, so chaining to it would put an unresolvable digest in every ancestry.
        if self._state is None:
            # The trust root still travels: a brain opened over a genesis document (or handed a
            # snapshot explicitly) commits its first content under the authority it already has.
            snapshot = Snapshot(
                modules={ref.memory_type: ref for ref in references},
                trust_root=self._snapshot.trust_root,
            )
        else:
            snapshot = self._snapshot.with_modules(references)
        self._advance(snapshot)
        return CommitResult(
            snapshot=snapshot,
            committed=committed,
            provenance=[block.block_id for block in provenance],
            roots=roots,
        )

    @property
    def travelling_indices(self) -> frozenset[MemoryType]:
        """
        Which modules would carry their vector index if this brain were published.

        A travelling index cannot be regenerated, so a brain holds one only if it built it in this process
        or restored it from a layer the layout already had. Publishing a module whose index is absent is a
        legitimate outcome, but it is one a caller should be able to see coming rather than discover from a
        consumer whose semantic search quietly got worse.

        The index is persisted when the artifact is materialized -- by :meth:`pack` or :meth:`push` -- so a
        process that ingests and exits without doing either loses it, and the next process cannot get it
        back. Pack before you exit, or push from the process that committed.

        Returns:
            frozenset[MemoryType]: Modules whose travelling index is present and publishable.
        """
        return frozenset(self._vouched)

    def rebuild_indices(self, memory_types: Iterable[MemoryType] | None = None) -> None:
        """
        Regenerate the structural indices from the installed composition.

        Every write already does this for the modules it touched, so this matters when the composition
        arrived by another route: a brain reopened in a new process, or a version installed from a
        registry. Both call it for you -- :meth:`open` and :meth:`pull` -- and it stays public because an
        index the SDK is never seen to refresh is an index a caller has no way to refresh either.

        **Only indices that report themselves rebuildable are touched.** An index with
        ``rebuildable = False`` travels inside its module's layer precisely because no client can
        regenerate it, so regenerating it here would replace what a peer published with whatever this
        client's engine happened to produce -- which is the failure the travelling mechanism exists to
        prevent (paper Section 6.3).

        Only what is readable is indexed. A block can be a verifiable member of a version and still not be
        resolvable, after a selective install or a redaction, and an index can only index what it can read.

        Args:
            memory_types (Iterable[MemoryType] | None): Which modules to rebuild. Defaults to every module
                that has a registered index and is installed.
        """
        wanted = list(memory_types) if memory_types is not None else list(self.indices)
        for memory_type in wanted:
            if memory_type not in self._snapshot.modules:
                continue
            rebuildable = [index for index in self.indices.get(memory_type, []) if index.rebuildable]
            if rebuildable:
                self._build(self.module(memory_type), rebuildable)

    def _rebuild_indices(self, module: Module) -> None:
        """
        Rebuild every index of a module, travelling ones included.

        This is the write path, where rebuilding all of them is right: the blocks are new, and the only
        client that can index them is this one. A travelling index left alone here would describe the
        version before the commit.
        """
        self._build(module, self.indices.get(module.memory_type, []))

    def _build(self, module: Module, indices: Sequence[Index]) -> None:
        """Feed a module's readable blocks to each index, reading them once rather than once per index."""
        if not indices:
            return

        blocks = [
            block_id
            for block_id in module.block_ids
            if block_id not in module.tombstones and module.store.is_resolvable(block_id)
        ]
        decoded = [module.get(block_id) for block_id in blocks]
        for index in indices:
            # The store is passed as a ContentReader: an index over blocks that name their content
            # cannot work from the blocks alone, and narrowing the type is what keeps it from writing.
            index.build(decoded, module.store)
            if not index.rebuildable:
                # Built from this composition by this client's own engine, so it describes the version and
                # can be published.
                self._vouched.add(module.memory_type)

    def _restore_travelling(self) -> None:
        """Load the travelling indices this brain's own layout already holds.

        A structural index is regenerated from the blocks. This one cannot be, so the only way a reopened
        brain gets it back is to find the layer it was published in -- and the layout records that:
        ``index.json`` names the manifests, and a manifest names its index layers.

        Without this, opening a brain and pushing it republishes an empty index annotated with a model tag.
        The consumer loads it, holds nothing, and has no way to tell.
        """
        for artifact in published_artifacts(self.store):
            manifest = artifact.manifest
            if manifest is None:
                continue
            if manifest.config.digest == self._snapshot.digest:
                bound_modules = self._snapshot.modules
            elif manifest.config.media_type == PROJECTION_MEDIA_TYPE:
                try:
                    projection = Projection.from_document(self.store.get_bytes(manifest.config.digest))
                except (BlockError, DistributionError):
                    continue
                if projection.source not in self._snapshot.parents or any(
                    projection.modules.get(kind) != reference for kind, reference in self._snapshot.modules.items()
                ):
                    continue
                bound_modules = self._snapshot.modules
            else:
                continue  # Unreadable, or a manifest for some other version of this brain.

            for memory_type in self.indices:
                try:
                    layer = self._validated_vector_index(manifest, bound_modules, memory_type)
                    if layer is None or not self.store.is_resolvable(layer.digest):
                        continue
                    self._load_index(memory_type, layer)
                except DistributionError as error:
                    # Opening is not a request to install anything, so an unusable layer must not strand
                    # the brain. It is skipped, but never loaded merely because a mutable local manifest
                    # names it: the signed snapshot remains the authority over the payload digest.
                    logging.getLogger(__name__).warning(
                        "ignoring the %s travelling index while opening this brain: %s",
                        memory_type.value,
                        error,
                    )
                    continue
            return

    def _travelling_index_binding(self, memory_type: MemoryType) -> tuple[str | None, OciDigest | None]:
        """Persist the exact travelling-index payload built for a new module reference."""
        travelling = [index for index in self.indices.get(memory_type, []) if not index.rebuildable]
        if not travelling or memory_type not in self._vouched:
            return None, None
        index = travelling[0]
        if not isinstance(index, TravellingIndex) or index.model_tag is None:
            return None, None
        payload = index.dump()
        return index.model_tag, self.store.put_bytes(payload)

    # --- Retention -------------------------------------------------------------

    def plan_drop(self, request: DropRequest) -> CascadePlan:
        """
        Work out what a drop would take with it, without writing anything.

        Args:
            request (DropRequest): What would be excluded.

        Returns:
            CascadePlan: The dependents, by module, and what could be re-derived instead.

        Raises:
            ProtocolError: If a named block is not in the module's composition.
        """
        self._require_members(request.blocks, request.memory_type)
        return plan_many(
            request.blocks,
            request.memory_type,
            self.modules(),
            Ledger.of(self.modules()),
            request.rederive_against,
        )

    def drop(self, request: DropRequest) -> DropResult:
        """
        Exclude blocks from a module, rebuilding its Merkle DAG and cascading through provenance.

        Blocks are not mutated. What changes is the composition: a new Merkle DAG over the survivors, a
        new root, indices rebuilt, and the removal recorded in provenance. Consumers of the new root
        never see the dropped block, while older retained roots keep verifying exactly as before -- which
        is the property that makes exclusion usable for wrong knowledge (paper Section 10.6).

        A canonical drop is privileged and always cascades, so one logical removal of evidence can
        publish several new module versions in a single commit.

        Args:
            request (DropRequest): What to exclude, by whom, and why.

        Returns:
            DropResult: The new snapshot, what left each module, and the new roots. When the cascade
            exceeds the policy's review threshold nothing is written and ``review_required`` is set.

        Raises:
            RetentionPolicyError: If the policy forbids the drop.
            AppendOnlyViolationError: If the module is append-only.
            ProtocolError: If a named block is not in the module's composition.
        """
        self.policy.authorize(RemovalMechanism.DROP, request.memory_type)
        plan = self.plan_drop(request)

        if self.policy.requires_review(plan.size):
            return DropResult(snapshot=self._snapshot, review_required=True)

        dropped: dict[MemoryType, list[BlockId]] = {
            request.memory_type: sorted(set(request.blocks), key=lambda value: value.hex)
        }
        for memory_type, blocks in plan.dependents.items():
            merged = {*dropped.get(memory_type, []), *blocks}
            dropped[memory_type] = sorted(merged, key=lambda value: value.hex)

        # Every module the cascade reaches must permit the removal, or a canonical drop could rewrite an
        # append-only module through the back door.
        for memory_type in dropped:
            self.policy.authorize(RemovalMechanism.DROP, memory_type)

        now = utc_timestamp()
        records = [
            ProvenanceBlock(
                record=RemovalRecord(
                    blocks=blocks,
                    mechanism=RemovalMechanism.DROP,
                    memory_type=memory_type,
                    actor=request.actor,
                    at=now,
                    reason=request.reason,
                    policy=request.policy_name,
                    cascaded_from=None if memory_type is request.memory_type else plan.origin,
                )
            )
            for memory_type, blocks in dropped.items()
        ]

        commit = self._write(blocks={}, provenance=records, without=dropped)
        return DropResult(
            snapshot=commit.snapshot,
            dropped=dropped,
            roots=commit.roots,
            provenance=commit.provenance,
        )

    def drop_by_producer(self, request: ProducerDropRequest) -> DropResult:
        """
        Drop everything a given producer made: batch invalidation (paper Section 10.3).

        Because provenance records the producer of each derived block, a drop can be stated over a set --
        everything from one ingestion, or everything one model version derived. That is the natural
        response to deliberately wrong knowledge introduced in bulk, and it reuses the same cascade
        rather than inventing a second mechanism.

        **One invalidation is one version.** Looping over :meth:`drop` per module published a snapshot
        each time, returned only the last one's result -- so everything dropped before it was invisible
        to the caller -- and left the earlier modules already committed if a later one hit the policy.
        Every module is therefore planned and authorized first, and then written once, which is the same
        guarantee :meth:`drop` gives for its own cascade.

        Args:
            request (ProducerDropRequest): Whose output to invalidate, where, and why.

        Returns:
            DropResult: The new snapshot, everything that left each module, and the new roots. Empty if
            the producer made nothing in the named modules. When the combined cascade exceeds the
            policy's review threshold nothing is written and ``review_required`` is set.

        Raises:
            RetentionPolicyError: If the policy forbids the drop in any module the cascade reaches.
                Checked before anything is written, so a refusal leaves the brain untouched.
        """
        modules = self.modules()
        ledger = Ledger.of(modules)
        made = ledger.made_by(request.producer)
        if not made:
            return DropResult(snapshot=self._snapshot)

        origins: dict[MemoryType, list[BlockId]] = {}
        for memory_type in request.memory_types:
            if not self._snapshot.has_module(memory_type):
                continue
            present = sorted(
                (block_id for block_id in made if block_id in self.module(memory_type)),
                key=lambda value: value.hex,
            )
            if present:
                origins[memory_type] = present

        if not origins:
            return DropResult(snapshot=self._snapshot)

        for memory_type in origins:
            self.policy.authorize(RemovalMechanism.DROP, memory_type)

        dropped: dict[MemoryType, set[BlockId]] = {kind: set(blocks) for kind, blocks in origins.items()}
        cascade = 0
        cascaded_from: dict[MemoryType, BlockId | None] = {}
        for memory_type, blocks in origins.items():
            plan = plan_many(blocks, memory_type, modules, ledger)
            cascade += plan.size
            for kind, dependents in plan.dependents.items():
                dropped.setdefault(kind, set()).update(dependents)
                cascaded_from.setdefault(kind, plan.origin)

        if self.policy.requires_review(cascade):
            return DropResult(snapshot=self._snapshot, review_required=True)

        # Every module the combined cascade reaches must permit the removal, or invalidating a
        # producer could rewrite an append-only module through the back door.
        for memory_type in dropped:
            self.policy.authorize(RemovalMechanism.DROP, memory_type)

        excluded = {kind: sorted(blocks, key=lambda value: value.hex) for kind, blocks in dropped.items()}
        now = utc_timestamp()
        records = [
            ProvenanceBlock(
                record=RemovalRecord(
                    blocks=blocks,
                    mechanism=RemovalMechanism.DROP,
                    memory_type=memory_type,
                    actor=request.actor,
                    at=now,
                    reason=request.reason,
                    policy=request.policy_name,
                    cascaded_from=None if memory_type in origins else cascaded_from.get(memory_type),
                )
            )
            for memory_type, blocks in excluded.items()
        ]

        commit = self._write(blocks={}, provenance=records, without=excluded)
        return DropResult(
            snapshot=commit.snapshot,
            dropped=excluded,
            roots=commit.roots,
            provenance=commit.provenance,
        )

    def supersede(
        self,
        block: BlockId,
        superseded: BlockId,
        memory_type: MemoryType,
        reason: str | None = None,
    ) -> SupersessionResult:
        """
        Record that one block takes precedence over another, without changing membership.

        The superseded block stays in the composition and keeps proving into the root; what changes is
        accessibility, so a query holds it back unless asked for it. This is the only removal path
        available to the episodic module, which is append-only by protocol.

        Args:
            block (BlockId): The block that takes precedence.
            superseded (BlockId): The block it replaces.
            memory_type (MemoryType): Which module both belong to.
            reason (str | None): Why the earlier block was superseded.

        Returns:
            SupersessionResult: The new snapshot and the record written.

        Raises:
            ProtocolError: If either block is not in the module's composition, or they are the same block.
        """
        self.policy.authorize(RemovalMechanism.SUPERSEDE, memory_type)
        if block == superseded:
            raise ProtocolError(f"a block cannot supersede itself ({block.short})")
        self._require_members([block, superseded], memory_type)

        record = ProvenanceBlock(
            record=SupersessionRecord(
                block=block,
                supersedes=superseded,
                actor=self.actor,
                at=utc_timestamp(),
                reason=reason,
            )
        )
        commit = self._write(blocks={}, provenance=[record])
        return SupersessionResult(snapshot=commit.snapshot, provenance=commit.provenance)

    def demote(
        self,
        block: BlockId,
        memory_type: MemoryType,
        reason: str | None = None,
    ) -> SupersessionResult:
        """
        Lower a block's retrieval priority without removing it.

        Recorded in the ledger rather than on the block, because a block is immutable: if accessibility
        were a field, demoting a block would change its ``block_id`` and make it a different block.

        The decay function is deliberately absent. The paper leaves it open (Section 12), so this records
        the decision and how much a demoted block is penalized -- or whether the penalty fades -- is a
        retrieval strategy the implementation owns. The built-in scan simply holds demoted blocks back.

        Args:
            block (BlockId): The block to demote.
            memory_type (MemoryType): Which module it belongs to.
            reason (str | None): Why.

        Returns:
            SupersessionResult: The new snapshot and the record written.
        """
        self.policy.authorize(RemovalMechanism.DEMOTE, memory_type)
        self._require_members([block], memory_type)

        record = ProvenanceBlock(
            record=DemotionRecord(
                block=block,
                actor=self.actor,
                at=utc_timestamp(),
                reason=reason,
            )
        )
        commit = self._write(blocks={}, provenance=[record])
        return SupersessionResult(snapshot=commit.snapshot, provenance=commit.provenance)

    def prune(self, dry_run: bool = True) -> PruneReport:
        """
        Reclaim blobs unreachable from every retained root.

        Pruning never decides what to forget -- a drop already did. It reclaims what no retained
        composition still needs, which is why it is irreversible yet harmless: nothing a retained root
        names is touched.

        Reachability follows what a snapshot names transitively, not only its block ids: the composition
        documents, and the observed bytes a canonical block describes. Reclaiming a source blob because no
        composition listed its digest directly would destroy evidence a retained root still points at.

        Args:
            dry_run (bool): Whether to report without deleting. Defaults to reporting, because pruning
                cannot be undone.

        Returns:
            PruneReport: What was reachable and what was reclaimed.
        """
        retained = self.history()
        # A layout has two kinds of root: the snapshots it retains, and the tags it publishes. The second
        # names the manifest and the packed layers, which no snapshot mentions -- so without it, packing an
        # artifact and then pruning leaves index.json pointing at bytes that are gone.
        keep = mark(retained, self.store) | reachable_from_tags(self.store)
        # A signature record is named by the signature index pointer, not by any snapshot or tag,
        # so without this a prune would reclaim it -- and a signature a garbage collection can
        # remove is not a signature. Records covering snapshots the prune drops go with them.
        keep |= reachable_signatures(self.store, keep)
        reclaimable = sweep(keep, self.store)

        if not dry_run:
            for digest in reclaimable:
                self.store.delete(digest)

        return PruneReport(
            retained_roots=len(retained),
            reachable=len(keep),
            reclaimed=reclaimable,
            dry_run=dry_run,
        )

    def redact(self, block: BlockId, memory_type: MemoryType, reason: str) -> RedactionResult:
        """
        Destroy a block's bytes while a retained root still names it.

        Redaction punches a hole in a composition that still names the block. The Merkle DAG references
        identities, not bytes, so deleting the bytes changes no hash and membership still verifies -- but
        reconstruction of that one block is forfeited. :meth:`resolvability` reports it as tombstoned
        rather than missing, so a lawful erasure is never mistaken for a corrupt store.

        This is not the cleanup path. Wrong or obsolete knowledge is dropped; redaction is for personal
        data, credentials, or licensed material that must disappear even from retained history.

        **Content another block still names survives.** Bytes are addressed by their hash, so two blocks
        that say different things about one source hold a single copy of it. Destroying everything this
        block names would take the other block's datum with it -- and that block stays a resolvable
        member of its composition, so nothing would report the loss. Only content no surviving block
        names is destroyed; the redacted block's own envelope always is. When this holds bytes back,
        ``redacted`` says so by not listing them.

        Two limits are worth restating. Low-entropy or enumerable content may still be recovered by
        guessing and hashing candidates while the ``block_id`` is kept. And erasure does not propagate
        across already-pulled copies: a revocation can be published, but a distributed brain can only
        signal destruction, not guarantee it.

        Args:
            block (BlockId): The block to redact.
            memory_type (MemoryType): Which module it belongs to.
            reason (str): The legal or safety basis.

        Returns:
            RedactionResult: What was destroyed and the record written.

        Raises:
            RetentionPolicyError: If the policy declares no redactable content.
            ProtocolError: If the block is not in the module's composition.
        """
        self.policy.authorize(RemovalMechanism.TOMBSTONE, memory_type)
        self._require_members([block], memory_type)

        # The envelope, plus whatever content the block names and nothing else names. Asked of every
        # module, not only canonical: a redaction that leaves the bytes behind for another memory type
        # is a redaction that did not happen, and the caller was told otherwise.
        named = self.module(memory_type).get(block).content_digests
        shared = self._content_named_elsewhere(block, memory_type)
        destroyed: list[Digest] = [block, *(digest for digest in named if digest.hex not in shared)]

        record = ProvenanceBlock(
            record=RemovalRecord(
                blocks=[block],
                mechanism=RemovalMechanism.TOMBSTONE,
                memory_type=memory_type,
                actor=self.actor,
                at=utc_timestamp(),
                reason=reason,
            )
        )
        commit = self._write(blocks={}, provenance=[record], tombstones={memory_type: [block]})

        # The record goes in first. A tombstone with no ledger entry would be indistinguishable from
        # corruption, which is the one thing Section 10.6 forbids.
        for digest in destroyed:
            self.store.tombstone(digest, reason)

        return RedactionResult(
            mechanism=RemovalMechanism.TOMBSTONE,
            redacted=destroyed,
            provenance=commit.provenance,
            snapshot=commit.snapshot,
        )

    def _content_named_elsewhere(self, block: BlockId, memory_type: MemoryType) -> set[str]:
        """The content digests some *other* installed block still names, as hex.

        Content is addressed by its hash, so two blocks stating different things about the same
        bytes hold one copy of them -- registering a source under two media types is enough to
        produce exactly that. Destroying everything the redacted block names would then take the
        survivor's datum with it, and the survivor stays a resolvable member of its composition, so
        ``verify`` still passes and nothing announces the loss.

        The block's own envelope is never spared: that is what was asked for. Only the content it
        shares with a block that was not redacted is.

        Args:
            block (BlockId): The block being redacted, excluded from the scan.
            memory_type (MemoryType): Which module it belongs to.

        Returns:
            set[str]: Hex digests of content other blocks still name.
        """
        shared: set[str] = set()
        for kind in self._snapshot.installed:
            module = self.module(kind)
            for other in module.block_ids:
                if other == block and kind is memory_type:
                    continue
                if not self.store.is_resolvable(other):
                    continue
                shared.update(digest.hex for digest in module.get(other).content_digests)
        return shared

    def _require_members(self, blocks: Iterable[BlockId], memory_type: MemoryType) -> None:
        """A removal has to name blocks the composition actually holds."""
        module = self.module(memory_type)
        absent = [block_id.short for block_id in blocks if block_id not in module]
        if absent:
            raise ProtocolError(
                f"cannot remove from the {memory_type.value} module: {', '.join(absent)} "
                f"{'is' if len(absent) == 1 else 'are'} not in its composition"
            )

    # --- Distribution ----------------------------------------------------------

    # --- Reconciliation --------------------------------------------------------

    def _sides(self, snapshot: Snapshot) -> tuple[dict[MemoryType, Composition | None], list[MemoryType]]:
        """One snapshot's compositions, and which of its modules could not be read here.

        A module a snapshot does not name and a module whose composition never travelled are different
        facts and reconciliation treats them differently, so they are returned separately. The first is
        ordinary -- selective installation produces it. The second means the transfer was incomplete, which
        is a diagnosis rather than a failure: Section 12.5 requires a contribution that shipped derived
        blocks without their canonical source to come back as a verdict and a piece of advice, not as a
        refusal to look.
        """
        compositions: dict[MemoryType, Composition | None] = {}
        untransferred: list[MemoryType] = []
        for memory_type in MemoryType:
            reference = snapshot.modules.get(memory_type)
            if reference is None:
                compositions[memory_type] = None
                continue
            if not self.store.is_resolvable(reference.composition):
                compositions[memory_type] = None
                untransferred.append(memory_type)
                continue
            composition = composition_at(self.store, snapshot, memory_type)
            # The composition document travels in the history layer, so readability of the *list*
            # no longer says the module arrived. What does is the blocks: one this store has never
            # seen -- as opposed to tombstoned, which is known-and-destroyed -- means the layer was
            # never fetched, and the module is a version this side can name but not open.
            if composition is not None and any(not self.store.has(block_id) for block_id in composition.block_ids):
                compositions[memory_type] = None
                untransferred.append(memory_type)
                continue
            compositions[memory_type] = composition
        return compositions, untransferred

    def _carried_verbatim(
        self,
        reconciled: Mapping[MemoryType, ModuleReconciliation],
        theirs: Snapshot,
    ) -> dict[MemoryType, ModuleRef]:
        """Modules named by a history but reconcilable on neither side, taken at their recorded root.

        This is Section 12.8 read literally: the modules the publisher does not hold take their roots from
        the remote unchanged. Rebuilding is neither possible nor needed -- a root is a complete statement of
        a version, and adopting one does not require holding what it commits to. Without this, reconciling
        from a partial install would quietly uninstall the modules it never fetched, which is the outcome
        the refusal it replaces existed to prevent.
        """
        carried = {}
        for memory_type in MemoryType:
            if memory_type in reconciled:
                continue
            reference = theirs.modules.get(memory_type) or self._snapshot.modules.get(memory_type)
            if reference is not None:
                carried[memory_type] = reference
        return carried

    def _require_shared_authority(self, theirs: Snapshot) -> None:
        """Refuse to reconcile histories that carry different trust roots.

        The one conflict that must not be surfaced as a candidate: unioning two key lists grants
        the union of both sides' permissions, which defeats the quorum rule outright (paper
        Section 12.5). A change of authority is resolved as an explicit governance act -- a
        trust-root revision under the quorum rule -- never as a merge.
        """
        ours = self._snapshot.trust_root.digest if self._snapshot.trust_root else None
        others = theirs.trust_root.digest if theirs.trust_root else None
        if ours != others:
            raise GovernanceConflictError(
                f"the histories carry different trust roots ({ours.short if ours else 'none'} here, "
                f"{others.short if others else 'none'} there) and reconciling them would grant the "
                f"union of both sides' permissions; resolve the change of authority first, as a "
                f"trust-root revision under the quorum rule"
            )

    def plan_reconcile(
        self,
        theirs: OciDigest,
        ancestor: OciDigest | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcilePlan:
        """
        Work out what joining another history would produce, without writing anything.

        The plan is the same whichever strategy is chosen, because all three land the same blocks. That is
        why it does not take one: its job is to inform the choice. It reports what Equation 1 produced per
        module, a verdict on every incoming block, and what each of the three strategies would cost in
        attribution.

        It is also the review. Reviewing a pull request means reading a diff; here the incoming blocks are
        candidates, the ingestion gate applies unchanged, and every one of them emerges with a verdict --
        so which parts of a contribution fit is known before anything is decided.

        Args:
            theirs (OciDigest): The other history's head, already held locally. Use :meth:`fetch` to
                retrieve one without disturbing this brain.
            ancestor (OciDigest | None): The snapshot to reconcile against. Defaults to the nearest one the
                two histories share.
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks. Defaults
                to the protocol's own set.

        Returns:
            ReconcilePlan: What would happen.

        Raises:
            SnapshotError: If ``theirs`` or the ancestor is not held here.
            NoCommonAncestorError: If the two histories share no ancestor, or if the ``ancestor`` given is
                not in both of them.
        """
        head = snapshot_at(self.store, theirs)
        self._require_shared_authority(head)
        origin = self.origin
        base_digest = common_ancestor(
            self.store,
            self.reachable_history(),
            head,
            theirs,
            hint=ancestor if ancestor is not None else (origin.snapshot if origin else None),
        )
        if ancestor is not None and base_digest != ancestor:
            raise NoCommonAncestorError(
                f"snapshot {ancestor.short} was given as the ancestor of {theirs.short} and this brain's "
                f"history, but it is not in both; the nearest shared snapshot is {base_digest.short}"
            )

        base = snapshot_at(self.store, base_digest)
        ancestors, _ = self._sides(base)
        ours, _ = self._sides(self._snapshot)
        their_sides, untransferred = self._sides(head)
        merged = reconciled_modules(ancestors, ours, their_sides)

        # The gate judges against the state the reconciliation would produce, including a provenance module
        # that already holds the records the contribution brought: the citations of an incoming v1 block
        # live in its derivation record, and the diagnosis of an absent one lives in a removal record.
        compositions = {kind: Composition(kind, result.block_ids) for kind, result in merged.items()}
        modules = {kind: Module(kind, self.store, composition) for kind, composition in compositions.items()}
        report = judge_incoming(
            {kind: result.incoming for kind, result in merged.items()},
            compositions,
            self.store,
            Ledger.of(modules),
            validators,
        )

        # Equation 1 is applied per module and is individually correct in each, which is exactly why it is
        # not sufficient: a block excluded in the canonical module leaves its dependents behind in the
        # semantic one, citing evidence the composition no longer holds. The gate catches that for blocks
        # arriving from the other history; it cannot catch it for blocks that were already here, because
        # nobody proposed them. The cascade a drop runs is what does, and it is the same cascade.
        cascaded = self._cascade_for(merged)
        withdrawn = self._withdrawn(merged, ours, cascaded)

        chain = snapshots_between(self.store, head, theirs, base_digest)
        replayable = [step for step in chain if is_reopenable(self.store, step)]
        return ReconcilePlan(
            ancestor=base_digest,
            theirs=theirs,
            modules=merged,
            incoming=report,
            cascaded=cascaded,
            withdrawn=withdrawn,
            attribution=attribution_table(len(chain), len(replayable)),
            collapsed=len(chain),
            replayable=len(replayable),
            untransferred=untransferred,
            authorship=self._offered_authorship(head),
            carried=self._carried_verbatim(merged, head),
        )

    def _offered_authorship(self, head: Snapshot) -> Authorship | None:
        """Who signed an incoming head, judged as a proposal rather than as a head.

        The stance is the whole point. The same signature by a key this brain's trust root does not
        list is an impersonation attempt when a registry serves it as the current state, and an
        ordinary contribution when someone offers it for review -- and a maintainer reading a plan is
        in the second situation by construction. Reporting it as unauthorized here would make an open
        project unable to describe the thing it does most often.
        """
        report = Authenticator(self.store).authenticate(
            head,
            current=self._snapshot.trust_root,
            stance=SnapshotStance.OFFERED,
        )
        return None if report.state is AuthorshipState.UNSIGNED else report.authorship()

    def _cascade_for(self, merged: Mapping[MemoryType, ModuleReconciliation]) -> dict[MemoryType, list[BlockId]]:
        """The cascade a reconciled set of modules implies, read off that set alone.

        Built over the compositions Equation 1 produced rather than the installed ones, so the same question
        can be asked of the whole contribution when planning and of one replayed version when writing.

        Args:
            merged (Mapping[MemoryType, ModuleReconciliation]): What Equation 1 produced per module.

        Returns:
            dict[MemoryType, list[BlockId]]: The surviving blocks whose evidence the result excludes.
        """
        compositions = {kind: Composition(kind, result.block_ids) for kind, result in merged.items()}
        modules = {kind: Module(kind, self.store, composition) for kind, composition in compositions.items()}
        return self._cascade_from(merged, modules, Ledger.of(modules))

    @staticmethod
    def _within(
        cascaded: Mapping[MemoryType, list[BlockId]],
        accepted: Mapping[MemoryType, list[BlockId]],
    ) -> dict[MemoryType, list[BlockId]]:
        """A step's cascade, narrowed to what the operator was shown and accepted.

        Their removals accumulate along their chain, so a step's cascade is always contained in the whole
        contribution's and this is a no-op. It is written down anyway because the direction it fails in
        matters: a block outside the accepted set stays, and the last step -- where it is inside the set --
        is what removes it. Nothing leaves on a step that nobody reviewed.

        Args:
            cascaded (Mapping[MemoryType, list[BlockId]]): This step's cascade.
            accepted (Mapping[MemoryType, list[BlockId]]): The cascade the plan reported.

        Returns:
            dict[MemoryType, list[BlockId]]: The intersection, in canonical leaf order.
        """
        narrowed = {}
        for memory_type, blocks in cascaded.items():
            allowed = set(accepted.get(memory_type, []))
            kept = [block for block in blocks if block in allowed]
            if kept:
                narrowed[memory_type] = kept
        return narrowed

    def _cascade_from(
        self,
        merged: Mapping[MemoryType, ModuleReconciliation],
        modules: dict[MemoryType, Module],
        ledger: Ledger,
    ) -> dict[MemoryType, list[BlockId]]:
        """Which surviving blocks cite evidence the reconciliation excluded.

        The cascade of Section 10.3, run over the compositions Equation 1 produced rather than over the
        installed ones. Reusing it rather than writing an evidence check for this case is the point: a
        reconciliation that removes evidence has the same consequence as a drop that removes it, and two
        implementations of one consequence would eventually disagree.

        It is not policy-gated, unlike :meth:`drop`. The removal already happened in the other history and
        Equation 1 is a statement about sets, not a request to remove something -- a policy that refused it
        here would not prevent the removal, only leave this brain unable to represent a history that
        contains it.
        """
        reached: dict[MemoryType, set[BlockId]] = {}
        for memory_type, result in merged.items():
            if not result.removed:
                continue
            plan = plan_many(result.removed, memory_type, modules, ledger)
            for kind, dependents in plan.dependents.items():
                reached.setdefault(kind, set()).update(dependents)

        surviving = {kind: set(result.block_ids) for kind, result in merged.items()}
        return {
            kind: sorted_leaves(blocks & surviving.get(kind, set()))
            for kind, blocks in reached.items()
            if blocks & surviving.get(kind, set())
        }

    @staticmethod
    def _withdrawn(
        merged: Mapping[MemoryType, ModuleReconciliation],
        ours: Mapping[MemoryType, Composition | None],
        cascaded: Mapping[MemoryType, list[BlockId]],
    ) -> dict[MemoryType, list[BlockId]]:
        """What this brain currently holds that the reconciliation would not name.

        Both causes at once, because to the operator they are one thing: a block the other history dropped,
        which exclusion's precedence in Equation 1 keeps out, and a block of theirs or ours that followed
        the evidence it cited.
        """
        leaving = {}
        for memory_type, result in merged.items():
            mine = ours.get(memory_type)
            if mine is None:
                continue
            final = set(result.block_ids) - set(cascaded.get(memory_type, []))
            gone = set(mine) - final
            if gone:
                leaving[memory_type] = sorted_leaves(gone)
        return leaving

    def reconcile(
        self,
        request: ReconcileRequest,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcileResult:
        """
        Join another history into this one, recording it the chosen way.

        The strategy is the caller's decision and there is no default. All three produce the same blocks,
        so choosing between them is choosing who stays on record as the author: a merge keeps the other
        side's snapshots and therefore their signature; a rebase and a squash mint new identities, and
        their work ends up signed by whoever reconciled. That may be exactly right for a small reviewed
        contribution, and it is not something this SDK will decide.

        Args:
            request (ReconcileRequest): Which history to join, how to record it, by whom, and why.
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks.

        Returns:
            ReconcileResult: What was committed, with the plan and the attribution that produced it.

        **It stops rather than proceeding without everything.** If any incoming block did not apply cleanly,
        nothing is written: what is open is recorded and the reconciliation waits. Committing the part that
        fits would be a decision about the rest -- the contributor loses those blocks and nobody was asked --
        and that is not what version control does with a conflict either.

        Args:
            request (ReconcileRequest): Which history to join, how to record it, by whom, and why.
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks.

        Returns:
            ReconcileResult: What was committed, with the plan and the attribution that produced it.

        Raises:
            ReconciliationHaltedError: If anything did not apply cleanly, or if another reconciliation is
                already unresolved. Nothing was written; see :meth:`reconcile_status`.
            NoCommonAncestorError: If the two histories share no ancestor.
            SnapshotError: If a block the result would name is not held here.
        """
        self._require_no_reconciliation(f"reconcile {request.theirs.short}")
        plan = self.plan_reconcile(request.theirs, request.ancestor, validators)

        if not plan.is_clean:
            self._put_reconcile_state(
                ReconcileState(
                    theirs=request.theirs,
                    ancestor=plan.ancestor,
                    strategy=request.strategy,
                    actor=request.actor,
                    reason=request.reason,
                    head=self._state.snapshot if self._state else self._snapshot.digest,
                )
            )
            open_questions = [verdict for verdict in plan.incoming.verdicts if not verdict.is_admissible]
            leaving = sum(len(blocks) for blocks in plan.withdrawn.values())
            said = []
            if open_questions:
                named = ", ".join(f"{verdict.block.short} ({verdict.status.value})" for verdict in open_questions[:5])
                said.append(
                    f"{len(open_questions)} of {len(plan.incoming.verdicts)} incoming blocks need a decision ({named})"
                )
            if leaving:
                said.append(
                    f"{leaving} block(s) this brain holds would be removed, so it needs reconcile_accept_removals()"
                )
            raise ReconciliationHaltedError(
                f"the reconciliation of {request.theirs.short} stopped: {'; and '.join(said)}. Nothing was "
                f"written. Inspect it with reconcile_status(), answer what is open, then "
                f"reconcile_continue() -- or reconcile_abort()."
            )

        return self._conclude(plan, request.theirs, request.strategy, {})

    def _conclude(
        self,
        plan: ReconcilePlan,
        theirs: OciDigest,
        strategy: ReconcileStrategy,
        resolutions: dict[BlockId, Resolution],
        accepted: RemovalAcceptance | None = None,
    ) -> ReconcileResult:
        """
        Write the reconciliation: compositions first, the head pointer last.

        The in-progress state is cleared *before* the write rather than after. An interruption then loses the
        decisions and leaves the brain exactly where it was, which is recoverable by redoing the work; the
        other order would leave a pointer describing a reconciliation that had already landed, and no reliable
        way to tell -- a rebase and a squash do not record the other history as a parent, so there is nothing
        to detect it by.

        A rebase writes one snapshot per replayed version, and those moves cannot be one transaction. Each is
        a valid version of the brain, so an interruption leaves a consistent brain partway along; it does not
        leave a resumable rebase.
        """
        self._put_reconcile_state(None)
        head = snapshot_at(self.store, theirs)
        chain = snapshots_between(self.store, head, theirs, plan.ancestor)
        # A version whose compositions never travelled cannot be restated, only passed through. Filtering
        # here rather than failing keeps a rebase possible over a fetched contribution, and
        # ``replayable`` on the plan is what says how much granularity was available to preserve.
        replayable = [step for step in chain if is_reopenable(self.store, step)]
        attribution = attribution_for(strategy, len(chain), len(replayable))

        # Their head may already be in this history, in which case there is no lineage to record. That is
        # not the same as nothing to do: a partial install reconciling with the tag it came from has no
        # divergence to settle and still has modules to adopt at the remote's roots, which is exactly what
        # Section 12.8 asks a publish-back to be. So containment silences the lineage, not the work.
        contained = not chain
        if contained and self._matches(plan, resolutions):
            return ReconcileResult(
                snapshot=self._snapshot,
                strategy=strategy,
                attribution=attribution,
                parents=list(self._snapshot.parents),
                roots={kind: reference.root for kind, reference in self._snapshot.modules.items()},
                plan=plan,
            )

        base = snapshot_at(self.store, plan.ancestor)
        ours, _ = self._sides(self._snapshot)
        ancestors, _ = self._sides(base)
        refused = self._refused_after(plan, resolutions)
        pending = self._precedence_records(plan, resolutions)

        steps = replay_steps(strategy, replayable) or [head]
        written: list[OciDigest] = []
        roots: dict[MemoryType, MerkleRoot] = {}
        # Records this reconciliation authored at an earlier step. Equation 1 is stated against the version
        # this brain was at, which is fixed for the whole replay, so a record written at one step is not in
        # any later step's arithmetic and would drop straight back out. Carrying the identifiers forward is
        # what keeps a replayed history additive.
        authored: dict[MemoryType, list[BlockId]] = {}
        cascaded_so_far: set[BlockId] = set()
        for position, step in enumerate(steps):
            last = position == len(steps) - 1
            # Every step is Equation 1 against the version that step of the other history was at, which is
            # what makes a replay deterministic here: there is no patch to apply, only a composition to
            # state. The last step reconciles against their head, so all three strategies end identically.
            partial = reconciled_modules(ancestors, ours, self._sides(step)[0])
            # The cascade follows *this step's* exclusions rather than the whole contribution's. A rebase
            # replays their history one version at a time, and the version that withdrew the evidence may be
            # the third of five: applying the consequence from the first would publish versions that exclude
            # a block whose evidence is still present, with nothing on record saying why.
            cascaded = self._within(self._cascade_for(partial), plan.cascaded)
            excluded = {kind: {*refused.get(kind, []), *cascaded.get(kind, [])} for kind in partial}
            members = {
                kind: [
                    *[block_id for block_id in result.block_ids if block_id not in excluded.get(kind, set())],
                    *authored.get(kind, []),
                ]
                for kind, result in partial.items()
            }
            fresh = self._cascade_records(
                {
                    kind: [block for block in blocks if block not in cascaded_so_far]
                    for kind, blocks in cascaded.items()
                },
                plan.theirs,
                accepted,
            )
            settled = [record for record in pending if last or _is_supported(record, members)]
            pending = [record for record in pending if record not in settled]
            snapshot, roots = self._write_reconciliation(
                members,
                plan.carried,
                [] if contained else (merged_parents(strategy, theirs) if last else []),
                retain=[theirs] if last and strategy is ReconcileStrategy.MERGE else [],
                extra=[*settled, *fresh],
            )
            for record in (*settled, *fresh):
                authored.setdefault(record.MEMORY_TYPE, []).append(record.block_id)
            cascaded_so_far.update(block for blocks in cascaded.values() for block in blocks)
            written.append(self._state.snapshot if self._state else snapshot.digest)

        return ReconcileResult(
            snapshot=self._snapshot,
            strategy=strategy,
            attribution=attribution,
            parents=list(self._snapshot.parents),
            snapshots=written,
            roots=roots,
            admitted={
                kind: sorted_leaves(set(plan.admitted(kind)) - set(refused.get(kind, []))) for kind in plan.modules
            },
            excluded=plan.excluded,
            plan=plan,
        )

    def _matches(self, plan: ReconcilePlan, resolutions: dict[BlockId, Resolution]) -> bool:
        """Whether carrying out a plan would leave this brain exactly where it is.

        Compared by root rather than by block list, because the root is what a version *is*: two
        compositions with the same root are the same version, and one with a different root is a new one
        however small the difference.
        """
        refused = self._refused_after(plan, resolutions)
        expected = {
            **{
                kind: Composition(kind, set(plan.admitted(kind)) - set(refused.get(kind, []))).root
                for kind in plan.modules
            },
            **{kind: reference.root for kind, reference in plan.carried.items()},
        }
        current = {kind: reference.root for kind, reference in self._snapshot.modules.items()}
        return expected == current

    def reconcile_status(self, validators: Sequence[Validator] | None = None) -> ReconcileStatus | None:
        """
        Where the reconciliation being resolved stands, if there is one.

        The plan is recomputed rather than remembered. A plan is a deterministic function of this brain's
        head, the other history, the ancestor and the blocks in the store, so recomputing it costs little and
        cannot report a judgment that has since stopped holding.

        Args:
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks. Pass the same
                set the reconciliation started with, or the verdicts will not be the ones it stopped on.

        Returns:
            ReconcileStatus | None: What is open and what has been decided, or ``None`` if nothing is in
            progress.
        """
        state = self._reconcile_state()
        if state is None:
            return None

        # The head cannot move while this is open -- every ordinary write is refused, and concluding clears
        # this pointer before it writes -- so a mismatch is not a race. It means the layout was changed by
        # something other than this API, and the decisions on record describe a reconciliation of a state
        # that is no longer here.
        current = self._state.snapshot if self._state else self._snapshot.digest
        if current != state.head:
            raise ReconciliationError(
                f"the reconciliation of {state.theirs.short} was started against snapshot {state.head.short} "
                f"but this brain is at {current.short}. Nothing moved it through this API, so the layout was "
                f"changed from outside; the decisions on record describe a state that is gone. Abandon it "
                f"with reconcile_abort()."
            )

        plan = self.plan_reconcile(state.theirs, state.ancestor, validators)
        open_questions = [verdict.block for verdict in plan.incoming.verdicts if not verdict.is_admissible]
        accepted = state.accepted_removals
        return ReconcileStatus(
            state=state,
            plan=plan,
            unresolved=[block for block in open_questions if block not in state.resolutions],
            resolved=[block for block in open_questions if block in state.resolutions],
            withdrawn=plan.withdrawn,
            # Compared against what was accepted, not merely present: an acceptance that covered a different
            # set of blocks answered a different question.
            removals_accepted=not plan.withdrawn or (accepted is not None and accepted.blocks == plan.withdrawn),
        )

    def reconcile_resolve(
        self,
        block: BlockId,
        kind: ResolutionKind,
        prefer: BlockId | None = None,
        reason: str | None = None,
        actor: Actor | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcileStatus:
        """
        Decide one of the questions a halted reconciliation is holding.

        Checked when it is recorded rather than when the reconciliation is concluded, so an impossible
        decision fails while you are still making it instead of after you have made all the others.

        Args:
            block (BlockId): Which incoming block to decide. Must be one the reconciliation is actually
                holding: deciding a block that applied cleanly would suggest the decision changed something.
            kind (ResolutionKind): What to do with it.
            prefer (BlockId | None): The winning successor, required for :attr:`ResolutionKind.PREFER` and
                meaningless otherwise.
            reason (str | None): Why.
            actor (Actor | None): Who decided. Defaults to this handle's actor.
            validators (Sequence[Validator] | None): Checks to apply, as in :meth:`reconcile_status`.

        Returns:
            ReconcileStatus: The state after recording it, so a caller can see what is left.

        Raises:
            ReconciliationError: If nothing is in progress, or the block is not one of the open questions.
            ResolutionRefusedError: If the decision would break an invariant rather than settle a conflict.
        """
        status = self.reconcile_status(validators)
        if status is None:
            raise ReconciliationError("no reconciliation is in progress, so there is nothing to resolve")
        verdict = next((entry for entry in status.plan.incoming.verdicts if entry.block == block), None)
        if verdict is None or verdict.is_admissible:
            raise ReconciliationError(
                f"{block.short} is not one of the questions this reconciliation is holding; the open ones are: "
                f"{', '.join(candidate.short for candidate in [*status.unresolved, *status.resolved]) or 'none'}"
            )

        self._require_resolvable(verdict, kind, prefer)
        state = status.state.with_resolution(
            block,
            Resolution(
                kind=kind,
                prefer=prefer,
                actor=actor if actor is not None else self.actor,
                reason=reason,
            ),
        )
        self._put_reconcile_state(state)
        resolved = self.reconcile_status(validators)
        assert resolved is not None  # just written
        return resolved

    def reconcile_accept_removals(
        self,
        reason: str | None = None,
        actor: Actor | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcileStatus:
        """
        State that the work this reconciliation removes may go.

        One answer rather than one per block, because the granularity would be false. Exclusion has
        precedence in Equation 1 -- a block the other history dropped does not come back because this one
        still held it -- so there is no per-block choice to offer. What is genuinely open is whether this
        reconciliation happens, and the alternative to accepting is :meth:`reconcile_abort`.

        Re-admitting a removed block afterwards remains possible and remains an ordinary commit, which is
        where a decision of that kind belongs: doing it inside a reconciliation would make the arithmetic
        depend on who was resolving it.

        Args:
            reason (str | None): Why the removals are accepted.
            actor (Actor | None): Who accepts them. Defaults to this handle's actor.
            validators (Sequence[Validator] | None): Checks to apply, as in :meth:`reconcile_status`.

        Returns:
            ReconcileStatus: The state after recording it.

        Raises:
            ReconciliationError: If nothing is in progress, or the reconciliation removes nothing.
        """
        status = self.reconcile_status(validators)
        if status is None:
            raise ReconciliationError("no reconciliation is in progress, so there is nothing to accept")
        if not status.withdrawn:
            raise ReconciliationError(
                f"the reconciliation of {status.state.theirs.short} removes nothing this brain holds, so "
                f"there is nothing to accept"
            )

        state = status.state.with_acceptance(
            RemovalAcceptance(
                blocks=status.withdrawn,
                actor=actor if actor is not None else self.actor,
                reason=reason,
            )
        )
        self._put_reconcile_state(state)
        accepted = self.reconcile_status(validators)
        assert accepted is not None  # just written
        return accepted

    def reconcile_continue(self, validators: Sequence[Validator] | None = None) -> ReconcileResult:
        """
        Conclude the reconciliation now that its questions are answered.

        Args:
            validators (Sequence[Validator] | None): Checks to apply, as in :meth:`reconcile_status`.

        Returns:
            ReconcileResult: What was committed.

        Raises:
            ReconciliationError: If nothing is in progress.
            ReconciliationBlockedError: If a question is still open. Section 12.4 forbids committing while a
                candidate is undecided: the protocol declined to decide, and committing would decide for it.
        """
        status = self.reconcile_status(validators)
        if status is None:
            raise ReconciliationError("no reconciliation is in progress, so there is nothing to continue")
        if status.unresolved:
            named = ", ".join(block.short for block in status.unresolved[:5])
            raise ReconciliationBlockedError(
                f"{len(status.unresolved)} question(s) are still open ({named}); decide them with "
                f"reconcile_resolve() before continuing"
            )
        if not status.removals_accepted:
            leaving = sum(len(blocks) for blocks in status.withdrawn.values())
            raise ReconciliationBlockedError(
                f"this reconciliation removes {leaving} block(s) this brain holds and nothing has said that "
                f"is acceptable; call reconcile_accept_removals() or reconcile_abort()"
            )

        return self._conclude(
            status.plan,
            status.state.theirs,
            status.state.strategy,
            status.state.resolutions,
            status.state.accepted_removals,
        )

    def reconcile_abort(self) -> None:
        """
        Abandon the reconciliation being resolved, discarding its decisions.

        Nothing is undone because nothing was written: a halted reconciliation never touched a composition or
        the head pointer. The blocks it fetched stay in the store, unreachable from any root, for a prune to
        reclaim -- the ordinary fate of anything no version names.

        Raises:
            ReconciliationError: If nothing is in progress.
        """
        if self._reconcile_state() is None:
            raise ReconciliationError("no reconciliation is in progress, so there is nothing to abandon")
        self._put_reconcile_state(None)

    @staticmethod
    def _require_resolvable(verdict: BlockVerdict, kind: ResolutionKind, prefer: BlockId | None) -> None:
        """Refuse a decision that would break an invariant rather than settle a conflict.

        Rejecting is always available: declining a contribution needs no justification a protocol can check.
        Admitting is not. A ``REJECTED`` block is malformed, unreadable, or cites evidence the composition does
        not hold, and the last of those breaks R1 in a way nothing downstream would catch -- ``verify``
        recomputes hashes and compositions, not citations across modules. So the refusal names the operation
        that fixes the cause instead, and that operation is an ordinary commit, which is where a decision to
        re-admit removed evidence belongs.
        """
        if kind is ResolutionKind.REJECT:
            return

        if kind is ResolutionKind.PREFER:
            contenders = _contenders(verdict)
            if not contenders:
                raise ResolutionRefusedError(
                    f"{verdict.block.short} is not a precedence question, so there is nothing to prefer; it is "
                    f"{verdict.status.value}"
                )
            if prefer is None or prefer not in contenders:
                named = ", ".join(sorted(block.short for block in contenders))
                raise ResolutionRefusedError(
                    f"prefer must name one of the competing successors of {verdict.block.short} ({named}); "
                    f"got {prefer.short if prefer else 'nothing'}"
                )
            return

        if verdict.status is ValidationStatus.REJECTED:
            causes = "; ".join(f"{issue.code}: {issue.detail}" for issue in verdict.issues)
            raise ResolutionRefusedError(
                f"{verdict.block.short} cannot be admitted by decision -- {causes}. A block whose evidence the "
                f"composition does not hold cannot be audited against its source, and no later check would "
                f"notice. Fix the cause instead: re-admit the evidence that was removed, or register a "
                f"replacement and re-derive against it. Both are ordinary commits, so abandon this "
                f"reconciliation first with reconcile_abort()."
            )

    def _refused_after(
        self,
        plan: ReconcilePlan,
        resolutions: dict[BlockId, Resolution],
    ) -> dict[MemoryType, list[BlockId]]:
        """Which blocks the result excludes, once the decisions are applied.

        The gate's refusals are the starting point; an ``ADMIT`` withdraws one and a ``REJECT`` confirms it.
        Nothing else can move a block into the result, because nothing else was offered.
        """
        refused: dict[MemoryType, list[BlockId]] = {}
        for verdict in plan.incoming.verdicts:
            if verdict.is_admissible:
                continue
            decision = resolutions.get(verdict.block)
            if decision is not None and decision.kind is not ResolutionKind.REJECT:
                continue
            refused.setdefault(verdict.memory_type, []).append(verdict.block)
        return refused

    def _cascade_records(
        self,
        cascaded: Mapping[MemoryType, Sequence[BlockId]],
        theirs: OciDigest,
        accepted: RemovalAcceptance | None,
    ) -> list[ProvenanceBlock]:
        """Removal records for the blocks the cascade takes with the evidence they cited.

        The exclusions Equation 1 itself performs need no record from here: the history that dropped those
        blocks wrote one, and it arrives as a provenance block like any other. What has no record yet is the
        consequence -- a block of this brain's that followed its evidence out -- so this writes it, in the
        same version as the composition it describes, and attributes it to whoever accepted the removals.

        Args:
            cascaded (Mapping[MemoryType, Sequence[BlockId]]): The blocks leaving in the version being
                written -- which for a rebase is that step's cascade and not the contribution's, so the
                record lands with the exclusion it explains rather than at the end of the replay.
            theirs (OciDigest): The history being reconciled from, named in the reason.
            accepted (RemovalAcceptance | None): Who accepted the removals, and why.

        Returns:
            list[ProvenanceBlock]: One record per module losing blocks, empty when nothing is.
        """
        actor = accepted.actor if accepted is not None else self.actor
        at = accepted.at if accepted is not None else utc_timestamp()
        reason = f"the evidence these blocks cite was withdrawn by the history reconciled from {theirs.short}"
        if accepted is not None and accepted.reason:
            reason = f"{reason}; accepted: {accepted.reason}"
        return [
            ProvenanceBlock(
                record=RemovalRecord(
                    blocks=list(blocks),
                    mechanism=RemovalMechanism.DROP,
                    memory_type=memory_type,
                    actor=actor,
                    at=at,
                    reason=reason,
                )
            )
            for memory_type, blocks in cascaded.items()
            if blocks
        ]

    def _precedence_records(
        self,
        plan: ReconcilePlan,
        resolutions: dict[BlockId, Resolution],
    ) -> list[ProvenanceBlock]:
        """The supersession edges that settle the precedence questions someone answered.

        Recorded as supersession rather than as a new kind of record, because that is what precedence already
        means here: naming a winner over a loser is exactly one more edge, and the ledger then resolves the
        chain without ambiguity. A record type invented for this would have added a second way to say the same
        thing.
        """
        records = []
        for verdict in plan.incoming.verdicts:
            decision = resolutions.get(verdict.block)
            if decision is None or decision.kind is not ResolutionKind.PREFER or decision.prefer is None:
                continue
            for loser in _contenders(verdict) - {decision.prefer}:
                records.append(
                    ProvenanceBlock(
                        record=SupersessionRecord(
                            block=decision.prefer,
                            supersedes=loser,
                            actor=decision.actor,
                            at=decision.at,
                            reason=decision.reason or f"precedence settled while reconciling {plan.theirs.short}",
                        )
                    )
                )
        return records

    def merge(
        self,
        theirs: OciDigest,
        reason: str,
        actor: Actor | None = None,
        ancestor: OciDigest | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcileResult:
        """
        Reconcile by recording both histories as parents.

        The only strategy that keeps the other side's snapshots in the history, and therefore the only one
        under which what they signed still covers something.

        Args:
            theirs (OciDigest): The other history's head.
            reason (str): Why this reconciliation is being made.
            actor (Actor | None): Who is reconciling. Defaults to this handle's actor.
            ancestor (OciDigest | None): The snapshot to reconcile against.
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks.

        Returns:
            ReconcileResult: What was committed.
        """
        return self._reconcile_as(ReconcileStrategy.MERGE, theirs, reason, actor, ancestor, validators)

    def rebase(
        self,
        theirs: OciDigest,
        reason: str,
        actor: Actor | None = None,
        ancestor: OciDigest | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcileResult:
        """
        Reconcile by replaying the other history onto this one, one snapshot at a time.

        Deterministic, and it mints new snapshot identities -- which invalidates any signature over the
        originals and any root already published. Legitimate before publication, under version control's
        own rule about rewriting public history.

        Args:
            theirs (OciDigest): The other history's head.
            reason (str): Why this reconciliation is being made.
            actor (Actor | None): Who is reconciling. Defaults to this handle's actor.
            ancestor (OciDigest | None): The snapshot to reconcile against.
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks.

        Returns:
            ReconcileResult: What was committed.
        """
        return self._reconcile_as(ReconcileStrategy.REBASE, theirs, reason, actor, ancestor, validators)

    def squash(
        self,
        theirs: OciDigest,
        reason: str,
        actor: Actor | None = None,
        ancestor: OciDigest | None = None,
        validators: Sequence[Validator] | None = None,
    ) -> ReconcileResult:
        """
        Reconcile by collapsing the other history's snapshots into one.

        More useful here than in version control, because an ingestion session mints many intermediate
        snapshots nobody cares about individually. It compacts snapshot history only: every provenance
        record those snapshots produced is a block in the provenance module, and Equation 1 keeps it.

        Args:
            theirs (OciDigest): The other history's head.
            reason (str): Why this reconciliation is being made.
            actor (Actor | None): Who is reconciling. Defaults to this handle's actor.
            ancestor (OciDigest | None): The snapshot to reconcile against.
            validators (Sequence[Validator] | None): Checks to apply to incoming derived blocks.

        Returns:
            ReconcileResult: What was committed.
        """
        return self._reconcile_as(ReconcileStrategy.SQUASH, theirs, reason, actor, ancestor, validators)

    def _reconcile_as(
        self,
        strategy: ReconcileStrategy,
        theirs: OciDigest,
        reason: str,
        actor: Actor | None,
        ancestor: OciDigest | None,
        validators: Sequence[Validator] | None,
    ) -> ReconcileResult:
        """The three named operations differ in one argument, so they share everything else."""
        return self.reconcile(
            ReconcileRequest(
                theirs=theirs,
                strategy=strategy,
                actor=actor if actor is not None else self.actor,
                reason=reason,
                ancestor=ancestor,
            ),
            validators,
        )

    def _write_reconciliation(
        self,
        members: dict[MemoryType, list[BlockId]],
        carried: dict[MemoryType, ModuleRef],
        merged: list[OciDigest],
        retain: Iterable[OciDigest] = (),
        extra: Sequence[ProvenanceBlock] = (),
    ) -> tuple[Snapshot, dict[MemoryType, MerkleRoot]]:
        """
        Publish one reconciled version: compositions first, pointer last.

        The write discipline is the one every other mutation follows -- blobs and composition documents are
        written before the pointer moves, so an interrupted reconciliation leaves the previous snapshot
        current and some unreachable documents a prune reclaims.

        Unlike a commit, membership is stated absolutely rather than as an addition: Equation 1 computes
        the whole composition, and expressing that as a delta against the current one would reintroduce
        the ordering question the set arithmetic exists to avoid.

        Args:
            members (dict[MemoryType, list[BlockId]]): The reconciled composition of every module the
                result names and this brain can rebuild.
            carried (dict[MemoryType, ModuleRef]): Modules taken at their recorded root instead, because
                neither side's composition is readable here. Their blocks are not required to be held:
                that is what publishing back from a partial install means.
            merged (list[OciDigest]): Additional parents to record, which is the other history for a merge
                and nothing for a rebase or a squash.
            extra (Sequence[ProvenanceBlock]): Records this reconciliation writes of its own -- the supersession
                edges that settle a precedence question someone answered. They land in the same version as the
                composition they describe, because a decision recorded in a later commit would leave one
                published version in which the ambiguity was unresolved.
            retain (Iterable[OciDigest]): Snapshots to keep reachable alongside this one.

        Returns:
            tuple[Snapshot, dict[MemoryType, MerkleRoot]]: The new snapshot and each module's new root.

        Raises:
            SnapshotError: If the result would name a block this brain does not hold. Writing it would
                produce a root that cannot be resolved, which is the one state a commit must never reach.
        """
        additions: dict[MemoryType, list[BlockId]] = {}
        for block in extra:
            self.store.put_block(block)
            additions.setdefault(block.MEMORY_TYPE, []).append(block.block_id)

        references: list[ModuleRef] = []
        roots: dict[MemoryType, MerkleRoot] = {}
        for memory_type, block_ids in {kind: [*ids, *additions.get(kind, [])] for kind, ids in members.items()}.items():
            absent = [block_id for block_id in block_ids if not self.store.has(block_id)]
            if absent:
                named = ", ".join(block.short for block in absent[:5])
                raise SnapshotError(
                    f"the reconciled {memory_type.value} composition names {len(absent)} block(s) this brain "
                    f"does not hold ({named}); fetch the other history before reconciling it"
                )
            module = Module(
                memory_type,
                self.store,
                Composition(memory_type, block_ids),
                self._index_map(memory_type),
                tombstones=(
                    block_id
                    for block_id in block_ids
                    if self.store.has(block_id) and not self.store.is_resolvable(block_id)
                ),
            )
            self._rebuild_indices(module)
            embedding_model, index_digest = self._travelling_index_binding(memory_type)
            references.append(module.persist(embedding_model=embedding_model, index_digest=index_digest))
            roots[memory_type] = references[-1].root

        for memory_type, reference in carried.items():
            references.append(reference)
            roots[memory_type] = reference.root

        if merged:
            snapshot = self._snapshot.reconciled(references, merged)
        else:
            snapshot = self._snapshot.with_modules(references)
        self._advance(snapshot, retain=retain)
        return snapshot, roots

    def pack(
        self,
        tag: str | None = None,
        modules: Iterable[MemoryType] | None = None,
    ) -> BrainManifest:
        """
        Materialize the current snapshot as an OCI artifact inside the local layout.

        One layer per module, the snapshot as the config blob, and the manifest written into
        ``index.json``. After this the directory is not merely an OCI *layout* -- it carries an
        *artifact*, so ``oras`` or any OCI tool can copy it without this SDK being involved.

        This is what makes publishing a transfer rather than a conversion: :meth:`push` packs and then
        moves blobs that already exist.

        **Publishing a subset.** A brain's sources can be gigabytes while its derived knowledge is
        kilobytes, and the right to derive from a book is not the right to redistribute the book. So
        ``modules`` narrows what is published -- but canonical cannot be omitted when a derived module is
        included. The paper's R1 makes canonical evidence the root of re-derivation: an artifact carrying
        semantic blocks whose citations point nowhere could not be audited or re-derived, only trusted,
        which is precisely what Section 4.2 says is lost without it. Publishing canonical or episodic
        alone is fine, because neither cites anything.

        **An index that cannot be rebuilt travels.** A module whose registered index reports
        ``rebuildable = False`` gets a second layer carrying that index's bytes, annotated with the model
        that produced it. Every other index is a deterministic function of the blocks, so a consumer
        regenerates it rather than downloading it (paper Section 6.3).

        Args:
            tag (str | None): Reference name to record in the index, so a tool can find the artifact
                by name rather than by digest.
            modules (Iterable[MemoryType] | None): Which modules to publish. Defaults to all installed.

        Returns:
            BrainManifest: The manifest, already stored as a blob.

        Raises:
            DistributionError: If a named module is not installed, or a derived module would be published
                without the canonical evidence it cites.
        """
        published = self._modules_to_publish(modules)

        layers = []
        schema_versions = {}
        for memory_type in published:
            module = self.module(memory_type)
            payload = pack_module(module)
            digest = self.store.put_bytes(payload)
            reference = self._snapshot.modules[memory_type]
            layers.append(Descriptor.for_module(reference, digest, len(payload)))
            # Declared on the manifest so a consumer can decide whether it has the schemas for this
            # brain before fetching a layer. Read from the blocks rather than from the registry: what
            # matters is what this artifact actually contains, not what this SDK happens to implement.
            schema_versions[memory_type] = module.schema_versions()

            index_layer = self._pack_index(memory_type, reference)
            if index_layer is not None:
                layers.append(index_layer)

        history = self._pack_history()
        if history is not None:
            layers.append(history)

        config_document = self._projection(published)
        config_bytes = config_document.canonical_bytes()
        config = Descriptor(
            media_type=(PROJECTION_MEDIA_TYPE if isinstance(config_document, Projection) else CONFIG_MEDIA_TYPE),
            digest=self.store.put_bytes(config_bytes),
            size=len(config_bytes),
        )
        manifest = build_manifest(
            self._snapshot,
            config,
            layers,
            annotations={
                ANNOTATION_SOURCE_SNAPSHOT: str(self._snapshot.digest),
                ANNOTATION_SCHEMA_VERSIONS: declare_schema_versions(schema_versions),
            },
            published=config_document.modules.values(),
        )
        manifest_bytes = manifest.to_bytes()
        manifest_digest = self.store.put_bytes(manifest_bytes)
        self._write_index(manifest_digest, len(manifest_bytes), tag)
        # Signatures travel with an exported brain: each record over the packed snapshot becomes
        # a signature manifest in the layout's own index -- the same object a push publishes as a
        # referrer, so the export and the registry carry one format (paper Section 8.8).
        self._write_signature_manifests(manifest, manifest_digest, len(manifest_bytes))
        return manifest

    def _published_record_snapshots(self, published: Snapshot) -> list[OciDigest]:
        """Snapshots whose signature records travel with a publish of ``published``.

        The records over the head claim the current state; the records over every first-parent
        REVISION ancestor -- and over a governed genesis -- are what lets a consumer re-run the
        custody walk, so a legitimately rotated brain does not arrive looking unauthorized.
        """
        digests: list[OciDigest] = []
        for position in walk_first_parents(self.store, published):
            if (
                position.digest == published.digest
                or position.role is SnapshotRole.REVISION
                or (position.role is SnapshotRole.GENESIS and position.snapshot.trust_root is not None)
            ):
                digests.append(position.digest)
        return digests

    def _write_signature_manifests(self, manifest: BrainManifest, digest: OciDigest, size: int) -> None:
        """Mirror every record over the packed snapshot into the layout index as signature manifests.

        The brain manifest is never touched: a countersignature added tomorrow is one more index
        entry, and the digest anyone pinned today still names exactly the artifact it named.
        """
        if not isinstance(self.store, OciLayoutStore):
            return
        records = [
            record
            for snapshot in self._published_record_snapshots(self._snapshot)
            for record in for_snapshot(self.store, snapshot)
        ]
        if not records:
            return
        self.store.put_bytes(EMPTY_CONFIG_BYTES)
        subject = Descriptor(media_type=MANIFEST_MEDIA_TYPE, digest=digest, size=size)
        index = self.store.index()
        entries = list(index.get("manifests", []))
        known = {entry.get("digest") for entry in entries}
        changed = False
        for record in records:
            payload = record.canonical_bytes()
            signature_manifest = build_signature_manifest(
                record_digest=OciDigest.of(payload),
                record_size=len(payload),
                key=record.key,
                snapshot=record.snapshot,
                subject=subject,
            )
            manifest_bytes = signature_manifest.to_bytes()
            signature_digest = self.store.put_bytes(manifest_bytes)
            if str(signature_digest) in known:
                continue
            entries.append(
                {
                    "mediaType": MANIFEST_MEDIA_TYPE,
                    "artifactType": SIGNATURE_MEDIA_TYPE,
                    "digest": str(signature_digest),
                    "size": len(manifest_bytes),
                    "annotations": dict(signature_manifest.annotations),
                }
            )
            known.add(str(signature_digest))
            changed = True
        if changed:
            index["manifests"] = entries
            self.store.write_index(index)

    async def _push_signatures(self, client: RegistryClient, reference: str, manifest: BrainManifest) -> int:
        """Publish the records over the pushed snapshot and its custody walk as referrers."""
        records = [
            record
            for snapshot in self._published_record_snapshots(self._snapshot)
            for record in for_snapshot(self.store, snapshot)
        ]
        if not records:
            return 0
        if not isinstance(client, RegistryReferrers):
            logging.getLogger(__name__).warning(
                "the transport for %s cannot carry referrers, so %d signature(s) stay local; consumers "
                "pulling through it will see this brain as unsigned",
                reference,
                len(records),
            )
            return 0
        self.store.put_bytes(EMPTY_CONFIG_BYTES)
        manifest_bytes = manifest.to_bytes()
        subject = Descriptor(media_type=MANIFEST_MEDIA_TYPE, digest=manifest.digest, size=len(manifest_bytes))
        for record in records:
            payload = record.canonical_bytes()
            await client.push_referrer(
                reference,
                build_signature_manifest(
                    record_digest=OciDigest.of(payload),
                    record_size=len(payload),
                    key=record.key,
                    snapshot=record.snapshot,
                    subject=subject,
                ),
                self.store,
            )
        return len(records)

    def _acceptable_record_snapshots(self, manifest: BrainManifest) -> set[OciDigest]:
        """Which snapshots a merged record may cover: the artifact's own custody walk.

        Exactly the set the verifier consults -- the config snapshot, its first-parent
        ancestry, and (for a subset artifact) the declared source head's ancestry. Never
        anything the local store merely happens to hold: a hostile listing must not get to
        attach records to unrelated local snapshots and poison their per-snapshot caps.
        """
        if manifest.config.media_type == PROJECTION_MEDIA_TYPE:
            try:
                source = Projection.from_document(self.store.get_bytes(manifest.config.digest)).source
            except (BlockError, DistributionError):
                return set()
            allowed: set[OciDigest] = {source}
            starts = [source]
        else:
            # Legacy reduced-snapshot projections used the annotation as their only binding.
            allowed = {manifest.config.digest}
            starts = [manifest.config.digest]
            declared = manifest.annotations.get(ANNOTATION_SOURCE_SNAPSHOT)
            if declared is not None:
                with suppress(IdentityError):
                    starts.append(OciDigest.parse(declared))
        for digest in starts:
            document = load_snapshot(self.store, digest)
            if document is None:
                continue
            allowed.add(digest)
            try:
                allowed.update(position.digest for position in walk_first_parents(self.store, document))
            except SnapshotError:
                # A manufactured chain (cycle, overlong): keep what resolved before it.
                continue
        return allowed

    async def _merge_signatures(self, client: RegistryClient, reference: str, manifest: BrainManifest) -> int:
        """Discover the referrers of a manifest and keep their records locally.

        A record that fails any structural check is skipped rather than fatal: a registry's
        referrers listing is unauthenticated input, and the verifier -- not the transport -- is
        where a bad signature becomes a finding. What is merged here is judged there.
        """
        if not isinstance(client, RegistryReferrers):
            return 0
        try:
            listing = await client.referrers(reference, manifest.digest, artifact_type=SIGNATURE_MEDIA_TYPE)
        except DistributionError as error:
            logging.getLogger(__name__).warning(
                "cannot list the signature referrers of %s: %s; continuing without remote signatures",
                reference,
                error,
            )
            return 0
        allowed = self._acceptable_record_snapshots(manifest)
        merged = 0
        full: set[OciDigest] = set()
        for descriptor in listing:
            if merged >= MAX_MERGED_PER_CALL:
                logging.getLogger(__name__).warning(
                    "the referrers listing of %s carries more than %d records; the rest are ignored",
                    reference,
                    MAX_MERGED_PER_CALL,
                )
                break
            try:
                signature_manifest = await client.pull_referrer(reference, descriptor.digest)
            except DistributionError:
                continue
            if signature_manifest.subject.digest != manifest.digest:
                continue
            record_layer = signature_manifest.record
            if not self.store.is_resolvable(record_layer.digest):
                try:
                    await client.pull_blob(reference, record_layer.digest, self.store)
                except DistributionError:
                    continue
            try:
                record = SignatureRecord.from_document(self.store.get_bytes(record_layer.digest))
            except (ValueError, BlockError, AuthenticityError):
                continue
            if record.snapshot not in allowed:
                continue
            if record.snapshot in full:
                continue
            try:
                store_record(self.store, record)
            except AuthenticityError as error:
                # Typically the per-snapshot record cap: locally-bounded state must win over an
                # unbounded listing, so the excess is dropped, never the install.
                full.add(record.snapshot)
                logging.getLogger(__name__).warning(
                    "not keeping further records over snapshot %s: %s", record.snapshot.short, error
                )
                continue
            merged += 1
        return merged

    async def _require_pin_holds(self, client: RegistryClient, reference: str, tag: str) -> None:
        """Judge a remote's trust root against the pin before transferring any module layer.

        The manifest's trust-root annotation is diagnostic only. It is written by whoever
        controls the registry, so equality with the pin proves nothing -- a forged annotation
        over an attacker config was precisely the bypass -- and inequality proves nothing
        either, because a quorum-approved rotation legitimately changes the digest. So the
        config, the history, and the signatures are always fetched (all small) and the full
        custody walk runs: a change of authority that followed the quorum rule is the mechanism
        working and is admitted; one that did not -- or a chain whose origin was withheld -- is
        refused **before** any module layer is paid for (paper Section 8.8).
        """
        pin = read_pin(self.store)
        if pin is None:
            return
        manifest = await client.resolve(reference, tag)
        if not self.store.is_resolvable(manifest.config.digest):
            await client.pull_blob(reference, manifest.config.digest, self.store)
        history = manifest.history
        if history is not None:
            if not self.store.is_resolvable(history.digest):
                await client.pull_blob(reference, history.digest, self.store)
            unpack_history(self.store.get_bytes(history.digest), self.store)
        resolved = self._resolve_config(manifest)
        await self._merge_signatures(client, reference, manifest)
        subject = resolved.source
        report = Authenticator(self.store).authenticate(subject, current=subject.trust_root)
        if report.has(FindingKind.PIN_BRAIN_MISMATCH):
            raise TrustRootMismatchError(
                f"{reference}:{tag} is not the brain this pin was taken for -- "
                f"{report.detail(FindingKind.PIN_BRAIN_MISMATCH)} -- refused before transferring any "
                f"module layer"
            )
        if report.has(FindingKind.TRUST_ROOT_MISMATCH):
            raise TrustRootMismatchError(
                f"{reference}:{tag} carries a trust root that neither matches the pinned "
                f"{pin.trust_root.short} nor descends from it through approved revisions; refused "
                f"before transferring any module layer. If this change of authority is expected, "
                f"re-pin explicitly and pull again"
            )
        for kind in (
            FindingKind.QUORUM_FAILURE,
            FindingKind.REVISION_CHANGED_CONTENT,
            FindingKind.REVISION_REGRESSED,
        ):
            if report.has(kind):
                raise QuorumFailureError(
                    f"{reference}:{tag} reaches the pinned trust root only through an unapproved "
                    f"revision -- {report.detail(kind)} -- refused before transferring any module layer"
                )
        if any(finding.kind is FindingKind.CHAIN_TRUNCATED and finding.blocking for finding in report.findings):
            raise UnauthorizedKeyError(
                f"{reference}:{tag} cannot prove where its authority came from -- "
                f"{report.detail(FindingKind.CHAIN_TRUNCATED)} -- refused before transferring "
                f"any module layer"
            )

    def _read_remote_snapshot(self, digest: OciDigest) -> Snapshot:
        """Parse a registry-supplied snapshot document, wrapping the failure for the wire.

        A config blob that does not fit this client's model is a compatibility fact, not a
        programming error: it reaches here from a manifest whose version gate passed, so the
        likeliest cause is an artifact from a newer SDK, and the refusal should say so instead
        of leaking a validation traceback.
        """
        try:
            return Snapshot.from_document(self.store.get_bytes(digest))
        except (ValueError, SnapshotError) as error:
            raise DistributionError(
                f"the artifact's snapshot document {digest.short} cannot be read by this client: "
                f"{error}; if the artifact was published by a newer SDK, upgrade pyboltzmann"
            ) from error

    def _resolve_config(self, manifest: BrainManifest) -> _ResolvedConfig:
        """Resolve a snapshot or projection config to the snapshot that authorizes it.

        The caller has already fetched and unpacked the history layer. A projection's ``source``
        field is the binding; the manifest annotation is deliberately ignored here because it is
        only a registry-controlled transfer hint.
        """
        if manifest.config.media_type == CONFIG_MEDIA_TYPE:
            document = self._read_remote_snapshot(manifest.config.digest)
            # Compatibility with v0.7 subset artifacts, whose config was a reduced snapshot and
            # whose only source binding was the annotation. New producers never take this path.
            source = self._resolve_legacy_source_anchor(document, manifest) or document
            return _ResolvedConfig(document=document, source=source)
        if manifest.config.media_type != PROJECTION_MEDIA_TYPE:
            raise DistributionError(
                f"artifact config has unsupported media type {manifest.config.media_type!r}; expected "
                f"{CONFIG_MEDIA_TYPE!r} or {PROJECTION_MEDIA_TYPE!r}"
            )

        projection = Projection.from_document(self.store.get_bytes(manifest.config.digest))
        projected_source = load_snapshot(self.store, projection.source)
        if projected_source is None:
            raise DistributionError(
                f"projection {projection.digest.short} binds source snapshot {projection.source.short}, "
                "but that snapshot is not resolvable from the artifact's history layer"
            )
        mismatched = [
            memory_type.value
            for memory_type, reference in projection.modules.items()
            if projected_source.modules.get(memory_type) != reference
        ]
        if mismatched:
            raise DistributionError(
                f"projection {projection.digest.short} is not a verbatim subset of source "
                f"{projected_source.digest.short}; mismatched module references: {', '.join(sorted(mismatched))}"
            )
        if projection.boltzmann != projected_source.boltzmann:
            raise DistributionError(
                f"projection protocol version {projection.boltzmann} disagrees with source snapshot "
                f"version {projected_source.boltzmann}"
            )
        return _ResolvedConfig(document=projection, source=projected_source)

    def _modules_to_publish(self, modules: Iterable[MemoryType] | None) -> list[MemoryType]:
        """Which modules an artifact will carry, refusing a subset that would strand a citation."""
        if modules is None:
            return list(self._snapshot.installed)

        wanted = [kind for kind in MemoryType if kind in set(modules)]
        absent = [kind.value for kind in wanted if not self._snapshot.has_module(kind)]
        if absent:
            installed = ", ".join(kind.value for kind in self._snapshot.installed) or "none"
            raise DistributionError(f"cannot publish {', '.join(absent)}: not installed. Installed: {installed}")
        if not wanted:
            raise DistributionError("cannot publish an artifact with no modules")

        derived = [kind for kind in wanted if kind.is_derived]
        if derived and MemoryType.CANONICAL not in wanted:
            raise DistributionError(
                f"cannot publish {', '.join(kind.value for kind in derived)} without canonical: those "
                f"blocks cite canonical evidence, and an artifact whose citations point nowhere could be "
                f"trusted but neither audited nor re-derived. Include canonical, or publish only modules "
                f"that cite nothing."
            )
        return wanted

    def _projection(self, published: list[MemoryType]) -> Snapshot | Projection:
        """
        The snapshot or projection document an artifact carries.

        For a complete publish this is the brain's own snapshot. A subset uses a distinct projection
        document: it binds the source digest and copies retained module references verbatim, without
        pretending the view has its own lineage, authority, timestamp, or signatures.
        """
        if set(published) == set(self._snapshot.installed):
            return self._snapshot
        return Projection(
            boltzmann=self._snapshot.boltzmann,
            source=self._snapshot.digest,
            modules={kind: self._snapshot.modules[kind] for kind in published},
        )

    def _pack_history(self) -> Descriptor | None:
        """A layer carrying this brain's snapshot documents, or ``None`` for a brain with no history yet.

        Published for the same reason a composition document is: the identity commits to something a
        consumer would otherwise be unable to reopen. A snapshot names its parents, and if only the head
        travels, those names resolve to nothing on the receiving side -- so the chain an audit walks stops
        at one link, and reconciliation is impossible for anyone but the publisher, because finding the
        ancestor two histories share means reading parents.

        The whole reachable history goes, not a recent window. A cutoff would decide, on the publisher's
        behalf, how far back a consumer is allowed to reconcile from -- and the thing being bounded is a
        few hundred bytes per version, against module layers that carry the knowledge itself.
        """
        documents = []
        compositions: dict[str, bytes] = {}
        for digest in sorted(self.reachable_history(), key=lambda value: value.hex):
            if not self.store.is_resolvable(digest):
                continue
            raw = self.store.get_bytes(digest)
            documents.append(raw)
            # The compositions each historical snapshot references travel too: without them a
            # consumer can verify an old version but never reopen or *difference* it, and the
            # required-scope computation is a difference (paper Section 8.5). Only what is still
            # resolvable goes -- a pruned composition is gone here as well as there.
            for reference in Snapshot.from_document(raw).modules.values():
                if reference.composition.hex not in compositions and self.store.is_resolvable(reference.composition):
                    compositions[reference.composition.hex] = self.store.get_bytes(reference.composition)
        if not documents:
            return None
        payload = pack_history(documents, compositions.values())
        return Descriptor.for_history(self.store.put_bytes(payload), len(payload), len(documents))

    def _pack_index(self, memory_type: MemoryType, reference: ModuleRef) -> Descriptor | None:
        """A layer for the one index kind a consumer cannot rebuild, or ``None`` if there is none.

        ``None`` also when this brain cannot vouch for the index. An index that was never built here and
        never loaded from a layer holds nothing, and dumping it would publish a layer that claims a vector
        index, carries none, and still says which model produced it -- a consumer loads it, holds nothing,
        and has no way to tell. Omitting the layer is the honest answer: ``plan_pull`` then reports no
        travelling index, which is true.
        """
        travelling = [index for index in self.indices.get(memory_type, []) if not index.rebuildable]
        if travelling and memory_type in self._vouched and not isinstance(travelling[0], TravellingIndex):
            index = travelling[0]
            raise DistributionError(
                f"the {index.kind.value} index for {memory_type.value} reports rebuildable=False but "
                f"cannot dump: an index that no client can rebuild has to be publishable, or the module "
                f"arrives without it and nothing can regenerate it"
            )
        if reference.index_digest is None:
            return None
        if not self.store.is_resolvable(reference.index_digest):
            raise DistributionError(
                f"the {memory_type.value} snapshot binds travelling index {reference.index_digest.short}, "
                "but its payload is not resolvable in this store"
            )
        payload = self.store.get_bytes(reference.index_digest)
        assert reference.embedding_model is not None
        annotations = {
            ANNOTATION_MEMORY_TYPE: memory_type.value,
            ANNOTATION_INDEX_KIND: IndexKind.VECTOR.value,
            ANNOTATION_EMBEDDING_MODEL: reference.embedding_model,
        }

        return Descriptor(
            media_type=VECTOR_INDEX_MEDIA_TYPE,
            digest=reference.index_digest,
            size=len(payload),
            annotations=annotations,
        )

    def _load_index(self, memory_type: MemoryType, layer: Descriptor) -> None:
        """
        Restore a travelling index a peer published, if this client registered one to receive it.

        An index built by a different embedding model is refused rather than loaded. Vectors from two
        models occupy different representation spaces, so mixing them would produce rankings that mean
        nothing -- and the annotation exists precisely so a consumer can tell.
        """
        candidates = [index for index in self.indices.get(memory_type, []) if not index.rebuildable]
        if not candidates:
            return

        index = candidates[0]
        published_model = layer.annotations.get(ANNOTATION_EMBEDDING_MODEL)
        if index.model_tag is not None and published_model is not None and index.model_tag != published_model:
            raise DistributionError(
                f"the published {memory_type.value} index was built by {published_model!r} but this client "
                f"expects {index.model_tag!r}; loading it would mix representation spaces"
            )
        if isinstance(index, TravellingIndex):
            index.load(self.store.get_bytes(layer.digest))
            # Loaded from a layer, so it describes a real version and may be republished.
            self._vouched.add(memory_type)

    def _write_index(self, digest: OciDigest, size: int, tag: str | None) -> None:
        """Point the layout's index at the manifest, replacing any entry for the same tag."""
        if not isinstance(self.store, OciLayoutStore):
            return
        index = self.store.index()
        descriptor: dict[str, Any] = {
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": ARTIFACT_TYPE,
            "digest": str(digest),
            "size": size,
        }
        if tag is not None:
            descriptor["annotations"] = {REF_NAME_ANNOTATION: tag}
        kept = [
            entry
            for entry in index.get("manifests", [])
            if tag is None or entry.get("annotations", {}).get(REF_NAME_ANNOTATION) != tag
        ]
        index["manifests"] = [*kept, descriptor]
        self.store.write_index(index)

    async def plan_pull(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
        *,
        ignore_vector_indices: bool = False,
    ) -> InstallPlan:
        """
        Work out what a pull would transfer, without downloading any module.

        Resolving a manifest is cheap and downloading it does not imply downloading anything else, so the
        cost of an install can be known before paying it. That is also the point of the incremental
        update: layers already held locally are reused by digest, so a plan over an existing brain reports
        only what actually moved.

        Args:
            client (RegistryClient): The transport.
            reference (str): Repository reference.
            tag (str): Which version to inspect.
            modules (Iterable[MemoryType] | None): Which modules are wanted. Defaults to everything the
                artifact carries.
            ignore_vector_indices (bool): Do not transfer published vector-index layers. The module
                blocks are still installed, and the caller becomes responsible for rebuilding a compatible
                vector index when it has an embedding model available.

        Returns:
            InstallPlan: What would be fetched and what would be reused.

        Raises:
            DistributionError: If a wanted module is not in the artifact.
        """
        manifest = await client.resolve(reference, tag)
        wanted = list(modules) if modules is not None else manifest.modules
        self._require_carried(manifest, wanted)

        fetch: list[MemoryType] = []
        reuse: list[MemoryType] = []
        for memory_type in wanted:
            layer = manifest.layer_for(memory_type)
            assert layer is not None  # checked above
            (reuse if self.store.is_resolvable(layer.digest) else fetch).append(memory_type)

        carried_indices = [
            memory_type for memory_type in wanted if (layer := manifest.vector_index_for(memory_type)) is not None
        ]
        indices = (
            []
            if ignore_vector_indices
            else [
                memory_type
                for memory_type in carried_indices
                if (layer := manifest.vector_index_for(memory_type)) is not None
                and not self.store.is_resolvable(layer.digest)
            ]
        )

        return InstallPlan(
            modules=wanted,
            fetch_layers=fetch,
            reuse_layers=reuse,
            fetch_vector_indices=indices,
            ignored_vector_indices=carried_indices if ignore_vector_indices else [],
            rebuild_indices=[
                index.kind.value
                for memory_type in wanted
                for index in self.indices.get(memory_type, [])
                if index.rebuildable
            ],
        )

    async def pull(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
        *,
        ignore_vector_indices: bool = False,
        verification: VerificationPolicy | None = None,
        allow_rollback: bool = False,
    ) -> Snapshot:
        """
        Fetch a published brain into this local layout, selectively.

        Only the module layers asked for are downloaded; the manifest is small and resolving it does not
        imply downloading anything else (paper Section 7.2). Layers already held are reused by digest,
        which is what makes an update incremental.

        Note that a partial install is read-only for derived knowledge: committing a semantic or
        procedural block requires the canonical module, because the validation gate checks that cited
        evidence is present.

        Args:
            client (RegistryClient): The transport.
            reference (str): Repository reference.
            tag (str): Which version to install.
            modules (Iterable[MemoryType] | None): Which modules are wanted. Defaults to everything the
                artifact carries.
            ignore_vector_indices (bool): Do not download or load published vector-index layers. This
                is an explicit compatibility escape hatch: modules and their Merkle roots are still
                installed and verified, but the caller must rebuild compatible vectors locally.
            verification (VerificationPolicy | None): This consumer's tolerances (paper Section
                8.10). Defaults to the paper's defaults: an unsigned brain is installed with a
                warning on first contact and refused when this brain was previously seen signed,
                and a head whose only valid signatures are ``propose``-scoped is refused. What is
                never configurable is the reporting.
            allow_rollback (bool): Install a served head that is a strict ancestor of the head
                already held. Defaults to refusal. An override always emits a ``ROLLBACK`` warning.

        Returns:
            Snapshot: The newly installed state.

        Raises:
            DistributionError: If a wanted module is not in the artifact, a wanted module uses a block
                schema this client does not implement, or a layer does not verify.
            RollbackError: If the served head is a strict ancestor of the local head and
                ``allow_rollback`` is false.
        """
        await self._require_pin_holds(client, reference, tag)
        manifest, resolved, references, _ = await self._retrieve(client, reference, tag, modules)
        source = resolved.source
        self._guard_pull_rollback(
            source,
            reference=reference,
            tag=tag,
            allow=allow_rollback,
        )
        await self._merge_signatures(client, reference, manifest)
        self._apply_verification_policy(source, verification, reference=reference)
        wanted = [reference_.memory_type for reference_ in references]

        for memory_type in wanted:
            # The one derived structure a model-agnostic client cannot rebuild, so it travels.
            index_layer = self._validated_vector_index(manifest, resolved.modules, memory_type, warn_legacy=False)
            if index_layer is not None and not ignore_vector_indices:
                if not self.store.is_resolvable(index_layer.digest):
                    await client.pull_blob(reference, index_layer.digest, self.store)
                self._load_index(memory_type, index_layer)

        complete = set(wanted) == set(manifest.modules)
        if complete and not resolved.is_projection:
            # Adopt the remote document verbatim. Rebuilding an equivalent one would give it a fresh
            # ``created_at`` and therefore a different digest, and the fast-forward check compares
            # digests -- so a push back to the same tag would look like a divergence when nothing
            # diverged at all.
            assert isinstance(resolved.document, Snapshot)
            installed = resolved.document
        else:
            # Chained to the version it was taken from, not parentless. A partial install *succeeds* that
            # version -- same roots, fewer modules -- and a snapshot that recorded no parent would leave a
            # consumer holding knowledge with no recorded origin, unable to say what it was installed from
            # and unable to be reconciled with the history it came from (paper Section 12.8).
            installed = Snapshot(
                boltzmann=source.boltzmann,
                modules={reference_.memory_type: reference_ for reference_ in references},
                parents=[source.digest],
                labels=source.labels,
                trust_root=source.trust_root,
            )

        origin = Origin(
            reference=reference,
            tag=tag,
            snapshot=source.digest,
            partial=resolved.is_projection or not complete,
        )
        advanced = self._advance(installed, origin=origin)

        # Record the artifact in the layout, the way ``pack`` does. Without it, everything the manifest
        # knows is lost when this process ends -- and the one thing only the manifest knows is where the
        # travelling index lives, which is exactly the thing no client can rebuild.
        document = manifest.to_bytes()
        self._write_index(self.store.put_bytes(document), len(document), tag)

        # What ``plan_pull`` reported under ``rebuild_indices``, actually done. The travelling index was
        # loaded above because no client can regenerate it; the structural ones are regenerated here,
        # because a consumer that installed a version and then searched it would otherwise query indices
        # that hold the version it had before the pull -- or nothing at all.
        self.rebuild_indices(wanted)
        return advanced

    def _guard_pull_rollback(
        self,
        served: Snapshot,
        *,
        reference: str,
        tag: str,
        allow: bool,
    ) -> None:
        """Refuse a strict ancestor of the held head, or report an explicit override.

        A missing local parent makes the ancestry question undecidable. The protocol permits a
        warning in that pruned-history case but does not permit treating uncertainty as proof of a
        rollback, so the pull continues with a distinguishable ``ROLLBACK_UNCHECKED`` report.
        """
        if self._state is None or served.digest == self._snapshot.digest:
            return
        relation = descends_from(self.store, self._snapshot, served.digest)
        if relation is None:
            logging.getLogger(__name__).warning(
                "ROLLBACK_UNCHECKED: cannot determine whether %s:%s at %s predates held head %s "
                "because local ancestry is pruned",
                reference,
                tag,
                served.digest.short,
                self._snapshot.digest.short,
            )
            return
        if not relation:
            return

        detail = (
            f"ROLLBACK: {reference}:{tag} serves {served.digest.short}, a strict ancestor of "
            f"held head {self._snapshot.digest.short}"
        )
        if not allow:
            raise RollbackError(f"{detail}; refused. Pass allow_rollback=True to override explicitly")
        logging.getLogger(__name__).warning("%s; ROLLBACK override accepted", detail)

    def _resolve_legacy_source_anchor(self, remote: Snapshot, manifest: BrainManifest) -> Snapshot | None:
        """Resolve the signed source of a legacy reduced-snapshot projection.

        A projection is parentless and nobody signs it; what its module roots commit to is
        byte-identical content the source head attests. The annotation names the source, the
        history layer carries its document, and this check makes the claim content-verified:
        every module the projection carries must match the source's exactly, along with the
        trust root. An honest publisher can never produce a mismatch, so one is refused
        outright rather than degraded.

        Returns:
            Snapshot | None: The anchor to judge authorship against, or ``None`` when the
            artifact is not a projection (or predates the annotation/history that would prove
            it is one -- those degrade to being judged as what they carry).

        Raises:
            DistributionError: If the named source is held but the artifact's content is not a
                subset of it.
        """
        declared = manifest.annotations.get(ANNOTATION_SOURCE_SNAPSHOT)
        if declared is None:
            return None
        try:
            source_digest = OciDigest.parse(declared)
        except IdentityError:
            return None
        if source_digest == remote.digest:
            return None
        anchor = load_snapshot(self.store, source_digest)
        if anchor is None:
            return None
        mismatched = (
            any(anchor.modules.get(kind) != reference for kind, reference in remote.modules.items())
            or remote.trust_root != anchor.trust_root
            or remote.boltzmann != anchor.boltzmann
            or remote.created_at != anchor.created_at
            or remote.labels != anchor.labels
        )
        if mismatched:
            raise DistributionError(
                f"the artifact claims to be a projection of {source_digest.short} but its content is "
                f"not a subset of it; refusing an anchor that lies"
            )
        return anchor

    def _apply_verification_policy(
        self,
        remote: Snapshot,
        verification: VerificationPolicy | None,
        *,
        reference: str,
    ) -> None:
        """The install-time gate the verification policy configures (paper Section 8.10).

        Three decisions, none of them about reporting: whether an unsigned brain may be
        installed -- warn-and-permit on first contact, refuse when this brain was previously
        seen signed, because a missing signature is then evidence of stripping -- and whether a
        ``propose``-scoped head may be treated as the current state, which a conforming
        implementation refuses unless the policy explicitly permits it (paper Section 12.6).
        Anything else unauthorized installs with a warning: what is not configurable is whether
        the result is reported, and the report is a call away either way.

        A subset artifact passes its resolved source here, not the projection document: the view
        is unsigned by design and its module roots are commitments the source attests.
        """
        removals = check_removal_invariant(self.store, remote)
        if not removals.is_valid:
            raise RemovalInvariantError(
                f"snapshot {remote.digest.short} has absences its reachable removal ledger does not "
                f"account for: {removals.detail}"
            )
        policy = verification if verification is not None else VerificationPolicy()
        judged = remote
        records = for_snapshot(self.store, judged.digest)
        if not records:
            previously = self._read_remotes().seen_signed.get(reference)
            refused = policy.unsigned is UnsignedPolicy.REFUSE or (
                previously is not None and policy.unsigned is not UnsignedPolicy.PERMIT
            )
            if refused:
                raise UnsignedBrainError(
                    "the remote head carries no signature"
                    + (
                        f" and {reference} was previously seen signed (at {previously.short}), so the "
                        f"absence is evidence of stripping rather than of never having signed"
                        if previously is not None
                        else ", and the policy refuses unsigned brains"
                    )
                    + "; pass a VerificationPolicy that permits unsigned to install it anyway"
                )
            if policy.unsigned is UnsignedPolicy.WARN:
                logging.getLogger(__name__).warning(
                    "installing %s unsigned: integrity verifies, authorship is unclaimed", remote.digest.short
                )
            return
        report = Authenticator(self.store, policy=policy).authenticate(judged, current=judged.trust_root)
        if report.has(FindingKind.REMOVAL_INVARIANT):
            raise RemovalInvariantError(report.detail(FindingKind.REMOVAL_INVARIANT))
        if report.is_proposal and not policy.allow_propose_head:
            raise InsufficientScopeError(
                f"the remote head {judged.digest.short} is signed only under propose: attributable, "
                f"verifiable, and explicitly not the published state. Treating it as current requires a "
                f"VerificationPolicy with allow_propose_head"
            )
        if report.state is AuthorshipState.AUTHORIZED:
            self._record_seen_signed(reference, judged.digest)
        else:
            logging.getLogger(__name__).warning(
                "installing %s with authorship %s: %s",
                remote.digest.short,
                report.state.value,
                "; ".join(finding.detail for finding in report.findings if finding.blocking) or "see the report",
            )

    async def _retrieve(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
    ) -> tuple[BrainManifest, _ResolvedConfig, list[ModuleRef], dict[MemoryType, list[BlockId]]]:
        """Download a published history's modules into the store, verifying each against the snapshot
        that names it.

        Writes content-addressed blobs and nothing else: no pointer move, no index work, no state. That
        is what lets ``pull`` and ``fetch`` share it -- the difference between them is entirely what
        happens *after* the bytes land.

        Returns:
            tuple: The manifest, its resolved config and source snapshot, the module references
            retrieved in the requested order, and the blocks each module layer contributed.
        """
        manifest = await client.resolve(reference, tag)
        wanted = list(modules) if modules is not None else manifest.modules
        self._require_carried(manifest, wanted)
        # Before the config blob, which is the first thing this method would otherwise download. A
        # brain whose blocks this client has no schema for is not installable, and finding that out
        # from a decode failure means finding it out after the transfer -- or later still, since
        # ``rebuild_indices`` only decodes for a module with a rebuildable index registered, so a
        # client with none installs the artifact cleanly and fails at the first query instead.
        require_supported_schemas(manifest, wanted)

        if not self.store.is_resolvable(manifest.config.digest):
            await client.pull_blob(reference, manifest.config.digest, self.store)

        # Before the modules, because it is what makes the retrieved snapshot's parents resolvable, and a
        # caller that fetched a history in order to reconcile against it needs that whether or not the
        # module layers turn out to verify.
        history = manifest.history
        if history is not None:
            if not self.store.is_resolvable(history.digest):
                await client.pull_blob(reference, history.digest, self.store)
            unpack_history(self.store.get_bytes(history.digest), self.store)

        resolved = self._resolve_config(manifest)
        for memory_type in wanted:
            self._validated_vector_index(manifest, resolved.modules, memory_type)

        references: list[ModuleRef] = []
        incoming: dict[MemoryType, list[BlockId]] = {}
        for memory_type in wanted:
            layer = manifest.layer_for(memory_type)
            assert layer is not None  # checked above
            expected = resolved.modules.get(memory_type)
            if expected is None:
                named = ", ".join(kind.value for kind in resolved.modules) or "none"
                raise DistributionError(
                    f"the artifact carries a {memory_type.value} layer but its config names no root for "
                    f"it; the config names: {named}. The manifest and its config disagree, so there is "
                    f"nothing to verify the layer against."
                )
            if not self.store.is_resolvable(layer.digest):
                await client.pull_blob(reference, layer.digest, self.store)

            # The manifest's layers and its config blob are two separate registry-supplied documents,
            # and nothing forces a registry to keep them consistent. Indexing straight into
            # the resolved config turned that into a bare KeyError, which is neither documented here nor
            # actionable by a caller.
            # Bounded by ``unpack_layer``'s own expansion ratio: the descriptor's size is the compressed
            # blob's, so it says nothing about what decompressing costs.
            composition = unpack_layer(
                self.store.get_bytes(layer.digest),
                self.store,
                tombstones=expected.tombstones or (),
            )
            if composition.root != expected.root:
                raise DistributionError(
                    f"the {memory_type.value} layer unpacks to root {composition.root.short} but the "
                    f"artifact's config names {expected.root.short}"
                )
            references.append(expected)

            # Against the installed composition rather than against the store, because that is the
            # question a caller has: what does this history hold that mine does not. A block the store
            # happens to keep from a version that dropped it is not something this history contributed.
            held = self._module_or_empty(memory_type)
            incoming[memory_type] = [block_id for block_id in composition.block_ids if block_id not in held]

        return manifest, resolved, references, incoming

    def _validated_vector_index(
        self,
        manifest: BrainManifest,
        modules: Mapping[MemoryType, ModuleRef],
        memory_type: MemoryType,
        *,
        warn_legacy: bool = True,
    ) -> Descriptor | None:
        """Resolve only an index layer whose payload digest the signed snapshot names."""
        layers = [layer for layer in manifest.layers if layer.is_vector_index and layer.memory_type is memory_type]
        reference = modules.get(memory_type)
        expected = reference.index_digest if reference is not None else None
        if expected is None:
            if layers and warn_legacy:
                logging.getLogger(__name__).warning(
                    "ignoring the %s vector index: the snapshot does not bind its payload digest",
                    memory_type.value,
                )
            return None
        if len(layers) != 1:
            raise DistributionError(
                f"the signed snapshot names {memory_type.value} index {expected.short}, but the "
                f"manifest carries {len(layers)} matching index layers; exactly one is required"
            )
        layer = layers[0]
        if layer.digest != expected:
            raise DistributionError(
                f"the signed snapshot names {memory_type.value} index {expected.short}, but the "
                f"artifact manifest substituted {layer.digest.short}"
            )
        return layer

    async def fetch(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
    ) -> FetchResult:
        """
        Retrieve a remote history without moving the local pointer.

        This is the step at which *nothing has changed yet* (paper Section 12.6). The blocks and the
        remote snapshot document land in the store, so both histories are readable locally, while the
        current snapshot -- and a published brain -- stay exactly as they were. It is the operation
        incorporating a contribution needs, and the reason it is separate from ``pull``: judging an
        incoming history should not require adopting it first.

        Because everything is content-addressed, the transfer is only the delta. A contributor's brain
        shares every block reachable from the snapshot they started at with this one, byte for byte, so
        only genuinely new blocks and the new snapshot move.

        No index is touched. A travelling vector index is bound to the root it was built over, and
        loading one for a history that is not installed would leave this brain holding an index bound to
        a root its snapshot does not name -- the stale-index failure. Indices are rebuilt if and when a
        reconciliation is committed.

        Args:
            client (RegistryClient): The transport.
            reference (str): Repository reference.
            tag (str): Which version to retrieve.
            modules (Iterable[MemoryType] | None): Which modules are wanted. Defaults to everything the
                artifact carries.

        Returns:
            FetchResult: The remote head, its digest, and what actually moved.

        Raises:
            DistributionError: If a wanted module is not in the artifact, a wanted module uses a block
                schema this client does not implement, or a layer does not verify against the snapshot
                that names it.
        """
        manifest, resolved, references, incoming = await self._retrieve(client, reference, tag, modules)
        # A contribution is a *signed* snapshot in someone else's repository; its records travel
        # with it, or the maintainer would judge attribution it cannot see.
        await self._merge_signatures(client, reference, manifest)
        # The remote snapshot document has to stay resolvable for a common-ancestor search to walk its
        # parents, and ``_retrieve`` already wrote it: it is the config blob.
        return FetchResult(
            reference=reference,
            tag=tag,
            snapshot=resolved.source,
            digest=resolved.source.digest,
            modules=[reference_.memory_type for reference_ in references],
            incoming=incoming,
        )

    async def push(
        self,
        client: RegistryClient,
        reference: str | None = None,
        tag: str | None = None,
        force: bool = False,
        modules: Iterable[MemoryType] | None = None,
    ) -> OciDigest:
        """
        Publish the current snapshot, refusing to overwrite work it does not contain.

        Before uploading, the remote tag is resolved and its snapshot checked against this brain's
        ancestry. If the remote is not an ancestor, the two histories diverged and pushing would drop
        whichever side lost -- so the push fails and says where they parted. Resolve it by pulling and
        re-committing.

        The upload itself is incremental: blobs the registry already holds are skipped, so an update
        that changed one module transfers one layer.

        Args:
            client (RegistryClient): The transport.
            reference (str | None): Repository reference. Defaults to the origin this brain was pulled
                from.
            tag (str | None): Tag to publish under. Defaults to the origin's tag.
            force (bool): Overwrite a diverged remote. Named for what it does.

        Returns:
            OciDigest: Digest of the pushed manifest.

        Raises:
            DistributionError: If there is nothing to publish, no reference is known, or the remote
                diverged and ``force`` was not set.
        """
        target, target_tag = self._push_target(reference, tag)
        if self._state is None:
            raise DistributionError("this brain has no snapshot to publish")

        if not force:
            await self._require_not_narrowing(client, target, target_tag)
            await self._require_fast_forward(client, target, target_tag)

        manifest = self.pack(tag=target_tag, modules=modules)
        digest = await client.push(target, target_tag, manifest, self.store)
        await self._push_signatures(client, target, manifest)
        self._advance(
            self._snapshot,
            # ``partial`` says this brain is missing modules the source had, which is what makes
            # republishing dangerous. Choosing to publish a subset says nothing of the kind: the local
            # brain is complete, so pushing the same projection again is a fast-forward, not a narrowing.
            origin=Origin(reference=target, tag=target_tag, snapshot=manifest.config.digest),
        )
        return digest

    @staticmethod
    def _require_carried(manifest: BrainManifest, wanted: Iterable[MemoryType]) -> None:
        """An artifact cannot hand over a module it does not carry."""
        missing = [kind.value for kind in wanted if manifest.layer_for(kind) is None]
        if missing:
            carried = ", ".join(kind.value for kind in manifest.modules) or "none"
            raise DistributionError(f"the artifact does not carry {', '.join(missing)}; it carries: {carried}")

    def _push_target(self, reference: str | None, tag: str | None) -> tuple[str, str]:
        origin = self.origin
        target = reference or (origin.reference if origin else None)
        target_tag = tag or (origin.tag if origin else None)
        if target is None or target_tag is None:
            raise DistributionError(
                "no repository to push to: this brain was not pulled from one, so pass a reference and a tag"
            )
        return target, target_tag

    async def _require_not_narrowing(self, client: RegistryClient, reference: str, tag: str) -> None:
        """Refuse to publish over a tag naming modules this snapshot omits.

        Those modules would silently disappear from the artifact. Stated over the snapshot rather than over
        the install: what makes a publish dangerous is that it names less than what is there, and whether
        the local brain happens to have been installed partially is a different question with a different
        answer (paper Section 12.8).

        The distinction matters because reconciliation resolves this case instead of working around it. A
        partial install that reconciles with the remote head takes the roots of the modules it does not hold
        from the remote unchanged, so the snapshot it then publishes names every module the remote named --
        and refusing that push, as a check keyed on ``origin.partial`` would, would forbid the very
        operation the protocol defines for it.
        """
        try:
            manifest = await client.resolve(reference, tag)
        except ReferenceNotFoundError:
            return  # Nothing is published here, so nothing can be narrowed.

        remote_modules = manifest.modules
        if manifest.config.media_type == PROJECTION_MEDIA_TYPE:
            if not self.store.is_resolvable(manifest.config.digest):
                await client.pull_blob(reference, manifest.config.digest, self.store)
            history = manifest.history
            if history is not None:
                if not self.store.is_resolvable(history.digest):
                    await client.pull_blob(reference, history.digest, self.store)
                unpack_history(self.store.get_bytes(history.digest), self.store)
            remote_modules = self._resolve_config(manifest).source.installed

        omitted = [kind.value for kind in remote_modules if not self._snapshot.has_module(kind)]
        if omitted:
            installed = ", ".join(kind.value for kind in self._snapshot.installed) or "none"
            raise DistributionError(
                f"{reference}:{tag} carries {', '.join(omitted)}, which this snapshot does not name (it names "
                f"{installed}); publishing it there would drop them. Reconcile with the remote head first -- "
                f"the modules this brain does not hold then take their roots from it unchanged -- or push to "
                f"a different tag, or pass force=True."
            )

    async def _require_fast_forward(self, client: RegistryClient, reference: str, tag: str) -> None:
        """Refuse a push that would drop a remote snapshot this brain does not contain."""
        try:
            manifest = await client.resolve(reference, tag)
        except ReferenceNotFoundError:
            return  # Nothing is published here, so there is nothing to overwrite.
        # Any other failure propagates. A guard that cannot read the remote has not checked anything, and
        # one that treats "I could not tell" as "nothing is there" would let an expired credential or a
        # failing registry turn into a push over somebody else's version.

        # A projection's config is not a version in anyone's history. Its document binds the source;
        # the annotation is only the compatibility path for v0.7 reduced-snapshot projections.
        if manifest.config.media_type == PROJECTION_MEDIA_TYPE:
            if not self.store.is_resolvable(manifest.config.digest):
                await client.pull_blob(reference, manifest.config.digest, self.store)
            remote = Projection.from_document(self.store.get_bytes(manifest.config.digest)).source
        else:
            source = manifest.annotations.get(ANNOTATION_SOURCE_SNAPSHOT)
            remote = OciDigest.parse(source) if source else manifest.config.digest
        # Reachability over every parent, not the first-parent chain: a history this brain merged is
        # contained in it, and publishing over it drops nothing.
        if remote in self.reachable_history():
            return

        raise DivergenceError(
            f"{reference}:{tag} is at snapshot {remote.short}, which is not in this brain's history; "
            f"the two diverged. Reconcile them -- fetch the remote and merge, rebase, or squash it -- "
            f"or pass force=True to overwrite the remote."
        )

    # --- Introspection ---------------------------------------------------------

    def history(self) -> list[Snapshot]:
        """
        The retained snapshots, most recent first.

        The chain is what a prune walks to decide what no version still needs, and what an audit walks
        to see how the brain got here.

        Returns:
            list[Snapshot]: The retained snapshots that are still resolvable.
        """
        if self._state is None:
            return []
        snapshots = []
        for digest in self._state.retained:
            if self.store.is_resolvable(digest):
                snapshots.append(Snapshot.from_document(self.store.get_bytes(digest)))
        return snapshots

    def state(self) -> dict[str, Any]:
        """
        The brain's mutable pointer, for tooling that inspects a layout.

        Returns:
            dict[str, Any]: The head pointer as stored, or an empty mapping for a fresh brain.
        """
        return json.loads(self._state.model_dump_json()) if self._state else {}
