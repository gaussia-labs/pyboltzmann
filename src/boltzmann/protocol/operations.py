"""The Boltzmann Protocol surface: what an implementation must offer.

An installed OCI Artifact is passive data. The protocol is the contract through which any conforming
client interacts with a brain, along two paths, query and ingestion. Boltzmann is delivered as a
protocol rather than as a deployable service: the brain is portable data, and any client that speaks
the protocol can read and extend it (paper Section 7).

This module states that contract as :class:`Protocol` classes, so "conforming" is something a type
checker can verify. **Nothing here is implemented.** These are the operations an implementation
provides; the SDK provides the types they exchange, the identities they compute, and the invariants
they must not break.

The surface is split because *read* and *extend* are separable, and most consumers only read:

* :class:`BrainReader` -- discovery, resolution, verification, query.
* :class:`BrainWriter` -- ingestion: register, delegate, validate, commit.
Catalog browsing belongs to :class:`BrainReader`; catalog classification belongs to
:class:`BrainWriter`, matching the operation table in paper Section 6.7.
* :class:`BrainRetention` -- drop, supersede, prune, redact.
* :class:`BrainDistribution` -- pack, push, pull, fetch.
* :class:`BrainReconciliation` -- merge, rebase, squash, and resolving what did not apply.
* :class:`BrainAuthenticity` -- sign, authenticate, pin, rotate, revoke.
* :class:`BoltzmannProtocol` -- all six contracts, for an implementation that offers everything.

A read-only client that satisfies :class:`BrainReader` is conforming. It does not have to pretend to
support writes it will refuse.

The protocol includes no LLM of its own. It governs structure, identity, validation, indices,
snapshots, and distribution; the semantic work is delegated to an interchangeable external model
through :class:`~boltzmann.ingest.proposer.CandidateProposer`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from boltzmann.authenticity.authenticator import AuthenticationReport
from boltzmann.authenticity.governance import RotationPlan, RotationResult
from boltzmann.authenticity.keys import SshPublicKey
from boltzmann.authenticity.pins import PinSource, TrustPin
from boltzmann.authenticity.policy import VerificationPolicy
from boltzmann.authenticity.record import SignatureRecord
from boltzmann.authenticity.scopes import Scope
from boltzmann.authenticity.signers import Signer
from boltzmann.authenticity.trust_root import TrustRoot
from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.catalog import (
    CatalogBrowseResult,
    CatalogDeclaration,
    CatalogPathView,
    ClassificationRequest,
)
from boltzmann.catalog_validation import ClassificationResult
from boltzmann.distribution.manifest import BrainManifest
from boltzmann.distribution.registry import FetchResult, RegistryClient
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.indices.base import Index, IndexKind
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.proposer import CandidateSet
from boltzmann.ingest.register import RegistrationRequest, RegistrationResult
from boltzmann.ingest.task import ProcessingTask
from boltzmann.ingest.validation import ValidationReport
from boltzmann.merkle.proof import InclusionProof
from boltzmann.module.module import Module
from boltzmann.module.snapshot import Snapshot
from boltzmann.query.evidence import EvidenceBundle
from boltzmann.query.request import Query
from boltzmann.reconcile.requests import ReconcilePlan, ReconcileRequest, ReconcileResult
from boltzmann.reconcile.resolution import ReconcileStatus, ResolutionKind
from boltzmann.retention.requests import (
    DropRequest,
    DropResult,
    ProducerDropRequest,
    PruneReport,
    RedactionResult,
    ResolvabilityReport,
    SupersessionResult,
)


@runtime_checkable
class BrainReader(Protocol):
    """Discovery, resolution, verification, and query. The minimum a conforming client offers."""

    # --- Discovery ------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """
        Discover installed modules, versions, and Merkle roots.

        Returns:
            Snapshot: The current state of the brain.
        """
        ...

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
        ...

    def module(self, memory_type: MemoryType) -> Module:
        """
        Open one installed module at the snapshot's version.

        Args:
            memory_type (MemoryType): Which module to open.

        Returns:
            Module: That module.

        Raises:
            SnapshotError: If the module is not installed.
        """
        ...

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
        """
        ...

    # --- Resolution and verification ------------------------------------------

    def resolve(self, block_id: BlockId) -> Block:
        """
        Resolve a ``block_id`` to its block, verifying its hash.

        Args:
            block_id (BlockId): The identity to resolve.

        Returns:
            Block: The decoded block.

        Raises:
            BlockNotFoundError: If the block is not held.
            BlockTombstonedError: If its bytes were redacted.
            BlockIntegrityError: If the stored bytes do not hash to ``block_id``.
        """
        ...

    def prove(self, block_id: BlockId, memory_type: MemoryType) -> InclusionProof:
        """
        Prove that a block belongs to the installed snapshot.

        Args:
            block_id (BlockId): The block whose membership is proven.
            memory_type (MemoryType): Which module it belongs to.

        Returns:
            InclusionProof: A proof of size ``O(log n)``.
        """
        ...

    def resolvability(self) -> ResolvabilityReport:
        """
        Report which blocks resolve, which were tombstoned, and which are missing.

        Required so that a removed block is never indistinguishable from a corrupted one
        (paper Section 10.6).

        Returns:
            ResolvabilityReport: The three-way classification.
        """
        ...

    def verify(self) -> bool:
        """
        Verify every installed module end to end.

        Returns:
            bool: Whether every composition is consistent and every resolvable block hashes to the
            identity it is filed under.
        """
        ...

    # --- Query ----------------------------------------------------------------

    def search(self, query: Query) -> EvidenceBundle:
        """
        Retrieve verified evidence for a declarative query.

        Must return knowledge blocks with their provenance and a retrieval score, never prose; must
        verify every returned block against the installed snapshot; and must treat no single index
        as authoritative (paper Section 9.2).

        Args:
            query (Query): The declarative request.

        Returns:
            EvidenceBundle: Verified matches.
        """
        ...

    def browse(self, classes: BlockId | Sequence[BlockId]) -> CatalogBrowseResult:
        """Browse a class or faceted class intersection, including descendant placements."""
        ...

    def catalog_path(self, schemes: Sequence[str]) -> CatalogPathView:
        """Build a virtual slash-separated read view over ordered catalog schemes."""
        ...


@runtime_checkable
class BrainWriter(Protocol):
    """Ingestion: preserve the source, delegate the interpretation, govern what is stored."""

    def register(self, data: bytes, request: RegistrationRequest) -> RegistrationResult:
        """
        Register a canonical source, recording provenance.

        Re-registering identical bytes must be a no-op, because identical content has one identity.

        Args:
            data (bytes): The original bytes, as observed.
            request (RegistrationRequest): Who is registering what, and under what policy.

        Returns:
            RegistrationResult: The canonical block's identity, and the commit if one happened.
        """
        ...

    def replace(self, data: bytes, request: RegistrationRequest, supersedes: BlockId) -> RegistrationResult:
        """
        Register a newer edition and record that it takes precedence.

        Register plus a supersession edge, with an optional drop of the old evidence -- never a
        mutation of bytes already stored (paper Section 8.1).

        Args:
            data (bytes): The new original's bytes.
            request (RegistrationRequest): Who is registering what.
            supersedes (BlockId): The canonical block being replaced.

        Returns:
            RegistrationResult: The new block's identity and the resulting commit.
        """
        ...

    def define_task(self, source: BlockId, allowed: Iterable[MemoryType]) -> ProcessingTask:
        """
        Define a processing task and output schema for an external LLM.

        Args:
            source (BlockId): The canonical block to interpret.
            allowed (Iterable[MemoryType]): Which kinds of block may be proposed. Canonical and
                provenance are never among them.

        Returns:
            ProcessingTask: The task to hand to a proposer.
        """
        ...

    def validate(self, candidates: CandidateSet, task: ProcessingTask) -> ValidationReport:
        """
        Validate candidate blocks before they are incorporated.

        Args:
            candidates (CandidateSet): What the external model proposed.
            task (ProcessingTask): The task the proposals answer.

        Returns:
            ValidationReport: One verdict per proposal.
        """
        ...

    def commit(self, report: ValidationReport) -> CommitResult:
        """
        Incorporate validated candidates, updating Merkle DAGs, indices, and the snapshot.

        The only write path, and one transaction: a failure part-way through must leave the previous
        snapshot as the current one. An external model can reach ``validate`` but never this without
        going through it.

        Args:
            report (ValidationReport): The verdicts to act on. Only ``VALIDATED`` candidates are
                committed.

        Returns:
            CommitResult: The new snapshot and the new roots.
        """
        ...

    def classify(
        self,
        request: ClassificationRequest | Sequence[CatalogDeclaration],
    ) -> ClassificationResult:
        """Validate and atomically commit catalog declarations and canonical placements."""
        ...


@runtime_checkable
class BrainRetention(Protocol):
    """Removal, in the four mechanisms Section 10 keeps distinct."""

    def drop(self, request: DropRequest) -> DropResult:
        """
        Exclude blocks from a module, rebuilding its Merkle DAG and cascading through provenance.

        A canonical drop is privileged and always cascades to every derived block that cited the
        evidence (paper Section 10.3).

        Args:
            request (DropRequest): What to exclude, by whom, and why.

        Returns:
            DropResult: The new snapshot, what left, and the new roots.

        Raises:
            RetentionPolicyError: If the policy forbids the drop.
            AppendOnlyViolationError: If the module is append-only.
        """
        ...

    def drop_by_producer(self, request: ProducerDropRequest) -> DropResult:
        """
        Drop everything a given producer made: batch invalidation (paper Section 10.3).

        Args:
            request (ProducerDropRequest): Whose output to invalidate, where, and why.

        Returns:
            DropResult: The new snapshot, what left, and the new roots.
        """
        ...

    def supersede(
        self,
        block: BlockId,
        superseded: BlockId,
        memory_type: MemoryType,
        reason: str | None = None,
    ) -> SupersessionResult:
        """
        Record that one block takes precedence over another, without changing membership.

        The only removal path available to the episodic module, and an optional soft path elsewhere.

        Args:
            block (BlockId): The block that takes precedence.
            superseded (BlockId): The block it replaces.
            memory_type (MemoryType): Which module both belong to.
            reason (str | None): Why the earlier block was superseded.

        Returns:
            SupersessionResult: The new snapshot and the record written.
        """
        ...

    def demote(self, block: BlockId, memory_type: MemoryType, reason: str | None = None) -> SupersessionResult:
        """
        Lower a block's retrieval priority without removing it.

        The decay function that governs demotion is a policy decision, not a protocol one.

        Args:
            block (BlockId): The block to demote.
            memory_type (MemoryType): Which module it belongs to.
            reason (str | None): Why the block was demoted.

        Returns:
            SupersessionResult: The new snapshot and the record written.
        """
        ...

    def prune(self, dry_run: bool = True) -> PruneReport:
        """
        Reclaim blocks unreachable from every retained root.

        Pruning never decides what to forget; a drop already did. It reclaims what no retained
        composition still needs.

        Args:
            dry_run (bool): Whether to report without deleting. Should default to reporting, because
                pruning cannot be undone.

        Returns:
            PruneReport: What was reachable and what was reclaimed.
        """
        ...

    def redact(self, block: BlockId, memory_type: MemoryType, reason: str) -> RedactionResult:
        """
        Destroy bytes that a retained root still names, under explicit policy.

        For law and safety, not for cleanup: wrong or obsolete knowledge is dropped. Membership still
        verifies afterwards, but reconstruction of that block is forfeited (paper Section 10.6).

        Args:
            block (BlockId): The block to redact.
            memory_type (MemoryType): Which module it belongs to.
            reason (str): The legal or safety basis.

        Returns:
            RedactionResult: What was destroyed and the record written.

        Raises:
            RetentionPolicyError: If the policy declares no redactable content.
        """
        ...


@runtime_checkable
class BrainDistribution(Protocol):
    """Packing, publishing, and installing, selectively and incrementally.

    Named ``pack``, ``push``, and ``pull`` rather than ``publish`` and ``install``. The operations are
    the ones Section 7 enumerates, but the shape they take is the one the paper describes in Section
    7.3: a brain moves between a remote artifact and a local layout, in both directions, with unchanged
    layers reused by digest. That is the shape everyone already has in their head from version control
    and container registries, and ``install`` would suggest something executable is being set up.
    """

    def pack(self, tag: str | None = None) -> BrainManifest:
        """
        Materialize the current snapshot as an OCI artifact locally, with no network involved.

        One layer per installed module, the snapshot as the config blob. Because the local brain is
        already an OCI layout, this is what makes publishing a transfer rather than a conversion -- and
        it means any OCI tool can copy the result without this SDK.

        Args:
            tag (str | None): Reference name to record, so a tool can find the artifact by name.

        Returns:
            BrainManifest: The manifest, already stored as a blob.
        """
        ...

    async def push(
        self,
        client: RegistryClient,
        reference: str | None = None,
        tag: str | None = None,
        force: bool = False,
    ) -> OciDigest:
        """
        Publish the current snapshot, refusing to overwrite work it does not contain.

        A conforming implementation must not silently drop a remote snapshot that is absent from the
        local history: it must refuse and say where the two parted. Detecting divergence is where this
        operation's obligation ends, not where the protocol stops -- deciding what to do next is
        :class:`BrainReconciliation`.

        Args:
            client (RegistryClient): The transport.
            reference (str | None): Repository reference. May default to where the brain was pulled from.
            tag (str | None): Tag to publish under.
            force (bool): Overwrite a diverged remote.

        Returns:
            OciDigest: Digest of the pushed manifest.
        """
        ...

    async def pull(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
    ) -> Snapshot:
        """
        Fetch a published brain into the local layout, selectively.

        A consumer must be able to install one module and update only what changed, which is what
        packaging each module as a separate blob is for (paper Sections 7.2 and 7.3).

        Args:
            client (RegistryClient): The transport.
            reference (str): Repository reference.
            tag (str): Which version to install.
            modules (Iterable[MemoryType] | None): Which modules are wanted. Defaults to all of them.

        Returns:
            Snapshot: The newly installed state.
        """
        ...

    async def fetch(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
    ) -> FetchResult:
        """
        Retrieve a remote history without moving the local pointer.

        Separate from ``pull`` because incorporating a contribution has a step at which nothing has
        changed yet: the maintainer holds two histories locally while the published brain is untouched,
        and only then judges the incoming blocks (paper Section 12.6). An operation that had to adopt a
        history in order to inspect it could not express that step.

        Args:
            client (RegistryClient): The transport.
            reference (str): Repository reference.
            tag (str): Which version to retrieve.
            modules (Iterable[MemoryType] | None): Which modules are wanted.

        Returns:
            FetchResult: The remote head, its digest, and what it holds that the local brain does not.
        """
        ...


@runtime_checkable
class BrainReconciliation(Protocol):
    """Joining two histories that advanced from a common ancestor (paper Section 12).

    Optional, and separable for the same reason reading and writing are: a consumer that installs and
    queries never reconciles anything. An implementation that offers ``push`` must still *detect*
    divergence and refuse -- that duty belongs to :class:`BrainDistribution` -- and offering these
    operations is what turns the refusal into something a maintainer can act on.

    The three strategies are one computation and three ways of recording it. They produce the same set of
    blocks, so an implementation must not choose between them on the caller's behalf, and must report the
    attribution consequence of the choice rather than let it happen silently.

    A conflict here is a validation failure rather than a differencing failure, so what an implementation owes
    an operator is the verdicts and a way to answer them -- not a merged state with markers in it. There is
    nothing to hand-edit: the unit is an immutable block, and the only questions are whether it enters and,
    where two histories disagree about precedence, which one wins.
    """

    def plan_reconcile(
        self,
        theirs: OciDigest,
        ancestor: OciDigest | None = None,
    ) -> ReconcilePlan:
        """
        Report what joining another history would produce, without writing anything.

        Must identify a common ancestor and must refuse with a distinguishable failure when the two
        histories share none: without one, a block present on one side and absent on the other is
        ambiguous between "they added it" and "I dropped it", and those demand opposite outcomes.

        Must judge every incoming block, so which parts of a contribution fit is known before anything is
        decided rather than inferred by reading a diff.

        Args:
            theirs (OciDigest): The other history's head, already held locally.
            ancestor (OciDigest | None): The snapshot to reconcile against, if known.

        Returns:
            ReconcilePlan: What would happen, and what each strategy would cost.
        """
        ...

    def reconcile(self, request: ReconcileRequest) -> ReconcileResult:
        """
        Join another history into this one, recording it the way the request asks.

        The structural reconciliation is set arithmetic over immutable blocks and is automatic; its result
        must then be validated as if it were an ingestion, and must not be committed while any candidate is
        still ``PENDING_REVIEW``. Only validated blocks enter the reconciled composition.

        Because the arithmetic runs one module at a time and the invariants run between them, excluding
        evidence in one module strands its dependents in another. An implementation **must** cascade through
        provenance exactly as a drop does, or it will publish a version in which a derived block cites
        evidence the composition does not hold -- a state that verifies, because verifying recomputes hashes
        and compositions rather than citations.

        Args:
            request (ReconcileRequest): Which history to join, how to record it, by whom, and why.

        Returns:
            ReconcileResult: What was committed.
        """
        ...

    def reconcile_status(self) -> ReconcileStatus | None:
        """
        Report the reconciliation being resolved, if there is one.

        Returns:
            ReconcileStatus | None: What is open and what has been decided, or ``None`` if nothing is in
            progress.
        """
        ...

    def reconcile_resolve(
        self,
        block: BlockId,
        kind: ResolutionKind,
        prefer: BlockId | None = None,
    ) -> ReconcileStatus:
        """
        Decide one of the questions a halted reconciliation is holding.

        An implementation **must not** admit a block whose cited evidence is absent from the reconciled
        composition, by this route or any other. Rejection there is not a policy preference: a derived block
        that cannot be audited against its source breaks R1, and no later check recovers it, because verifying
        a brain recomputes hashes and compositions rather than citations across modules. What an
        implementation offers instead is the operation that fixes the cause.

        Admitting a contradiction is a different matter and must be available: Section 12.4 treats a
        contradiction as information rather than a defect, so holding two claims that disagree is a state the
        protocol permits and a decision it does not make.

        Args:
            block (BlockId): Which incoming block to decide.
            kind (ResolutionKind): What to do with it.
            prefer (BlockId | None): The winning successor, for a precedence question.

        Returns:
            ReconcileStatus: The state after recording it.
        """
        ...

    def reconcile_accept_removals(self) -> ReconcileStatus:
        """
        State that the work this reconciliation removes may go.

        An implementation **must not** let a reconciliation remove blocks the brain holds without this being
        stated. Exclusion has precedence in Equation 1, so a block the other history dropped does leave --
        that is the rule and it is deliberate -- but applying it is a decision about work that is already
        here, and taking it silently is the same failure as deciding an undecided candidate.

        It is one answer and not one per block: there is no per-block choice to offer when exclusion wins by
        construction. Re-admitting a removed block remains possible and remains an ordinary commit.

        Returns:
            ReconcileStatus: The state after recording it.
        """
        ...

    def reconcile_continue(self) -> ReconcileResult:
        """
        Conclude the reconciliation now that its questions are answered.

        Must refuse while any candidate is undecided, for the reason Section 12.4 gives: the protocol declined
        to decide, and committing would decide for it.

        Returns:
            ReconcileResult: What was committed.
        """
        ...

    def reconcile_abort(self) -> None:
        """
        Abandon the reconciliation being resolved.

        Nothing is undone, because a halted reconciliation never wrote a composition or moved the pointer.
        """
        ...

    def merge(self, theirs: OciDigest, reason: str) -> ReconcileResult:
        """
        Reconcile into a snapshot naming both histories as parents.

        The only strategy that keeps the other side's snapshots in the history, and therefore the only one
        under which a signature they made still covers something.

        Args:
            theirs (OciDigest): The other history's head.
            reason (str): Why.

        Returns:
            ReconcileResult: What was committed.
        """
        ...

    def rebase(self, theirs: OciDigest, reason: str) -> ReconcileResult:
        """
        Reconcile by replaying the other history onto this one, minting new snapshot identities.

        Deterministic, because a snapshot is a complete statement of composition rather than a patch. It
        invalidates signatures over the originals and any root already published, so it is legitimate only
        before publication -- the same rule as any lineage rewrite.

        Args:
            theirs (OciDigest): The other history's head.
            reason (str): Why.

        Returns:
            ReconcileResult: What was committed.
        """
        ...

    def squash(self, theirs: OciDigest, reason: str) -> ReconcileResult:
        """
        Reconcile by collapsing the other history's snapshots into one.

        Must preserve every provenance record the collapsed snapshots produced: an implementation that
        discarded provenance while collapsing snapshots would be destroying the audit ledger to tidy a
        chain.

        Args:
            theirs (OciDigest): The other history's head.
            reason (str): Why.

        Returns:
            ReconcileResult: What was committed.
        """
        ...


@runtime_checkable
class BrainAuthenticity(Protocol):
    """Attesting to snapshots, and deciding whose attestations matter (paper Section 8).

    Claimable separately, and deliberately so: a consumer that recomputes integrity while holding
    no trust anchor is not a degraded client -- it is the zero-configuration case the protocol
    guarantees, and requiring signature verification for a reader to conform would make offline
    integrity conditional on configuration the protocol promises it does not need. What no
    implementation may do is claim an authenticity it did not check.

    The paper's ``init`` operation -- a genesis snapshot with its first trust root -- is a
    constructor rather than an instance capability, so it is not a member here; this SDK offers
    it as ``Brain.init``.
    """

    def sign(
        self,
        signer: Signer,
        snapshot: OciDigest | None = None,
        scopes: Iterable[Scope] | None = None,
    ) -> SignatureRecord:
        """
        Produce a detached signature over a snapshot under the protocol namespace.

        Args:
            signer (Signer): What produces the signature; the private key never enters the brain.
            snapshot (OciDigest | None): Which snapshot. Defaults to the current one.
            scopes (Iterable[Scope] | None): The claim to record. Defaults to the computed
                requirement; never the basis of any decision.

        Returns:
            SignatureRecord: The record, persisted beside the snapshot.
        """
        ...

    def authenticate(
        self,
        snapshot: OciDigest | None = None,
        policy: VerificationPolicy | None = None,
    ) -> AuthenticationReport:
        """
        Check signatures against the trust root in force at a snapshot's position.

        Reported separately from integrity, never collapsed into one boolean.

        Args:
            snapshot (OciDigest | None): Which snapshot. Defaults to the current one.
            policy (VerificationPolicy | None): This consumer's tolerances.

        Returns:
            AuthenticationReport: Every verdict and finding, with the summary derived.
        """
        ...

    def pin(self, trust_root: OciDigest | None = None, source: PinSource | None = None) -> TrustPin:
        """
        Record a trust root digest as the anchor for this brain.

        Args:
            trust_root (OciDigest | None): The digest to pin. Defaults to the current one --
                trust on first use.
            source (PinSource | None): How the pin was established.

        Returns:
            TrustPin: The recorded pin, held in consumer-side state and never in the artifact.
        """
        ...

    def plan_rotate(self, trust_root: TrustRoot) -> RotationPlan:
        """
        Build a trust-root revision without advancing the head, for multi-party signing.

        Args:
            trust_root (TrustRoot): The new key list.

        Returns:
            RotationPlan: The exact bytes every countersignature must cover.
        """
        ...

    def countersign(self, document: bytes, signer: Signer) -> SignatureRecord:
        """
        Inspect a governance document someone else built, and sign its exact bytes.

        Args:
            document (bytes): A revision snapshot's canonical bytes, verbatim as received.
            signer (Signer): What produces the signature.

        Returns:
            SignatureRecord: The record to send back to the initiator.
        """
        ...

    def rotate(
        self,
        trust_root: TrustRoot | None = None,
        signers: Sequence[Signer] = (),
        records: Sequence[SignatureRecord] = (),
        plan: RotationPlan | None = None,
    ) -> RotationResult:
        """
        Commit a trust-root revision, under the quorum rule.

        Args:
            trust_root (TrustRoot | None): The new key list, when building fresh.
            signers (Sequence[Signer]): Local signers.
            records (Sequence[SignatureRecord]): Signatures collected elsewhere.
            plan (RotationPlan | None): The planned document those records cover.

        Returns:
            RotationResult: What took effect. A failed quorum advances nothing.
        """
        ...

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

        Args:
            key (SshPublicKey | str): The key, by object, fingerprint, or authorized_keys line.
            signers (Sequence[Signer]): Who signs the revision.
            records (Sequence[SignatureRecord]): Signatures collected elsewhere.
            retired_from (int | None): The revision the key stops being authorized at.
            compromised_from (OciDigest | None): The snapshot its signatures are withdrawn from.

        Returns:
            RotationResult: The revision that recorded it.
        """
        ...

    def signatures(self, snapshot: OciDigest | None = None) -> list[SignatureRecord]:
        """
        The signature records held over a snapshot.

        Args:
            snapshot (OciDigest | None): Which snapshot. Defaults to the current one.

        Returns:
            list[SignatureRecord]: The records, possibly empty.
        """
        ...

    def add_signature(self, record: SignatureRecord) -> OciDigest:
        """
        Keep a signature record someone else produced.

        Args:
            record (SignatureRecord): The record to keep.

        Returns:
            OciDigest: The record blob's content address.
        """
        ...


@runtime_checkable
class BoltzmannProtocol(
    BrainReader,
    BrainWriter,
    BrainRetention,
    BrainDistribution,
    BrainReconciliation,
    BrainAuthenticity,
    Protocol,
):
    """An implementation that offers the whole protocol.

    Conforming to this is not required. A read-only client satisfies :class:`BrainReader` and is
    conforming for what it claims to do.
    """
