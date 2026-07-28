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
* :class:`BrainRetention` -- drop, supersede, prune, redact.
* :class:`BrainDistribution` -- pack, push, pull.
* :class:`BoltzmannProtocol` -- all four, for an implementation that offers everything.

A read-only client that satisfies :class:`BrainReader` is conforming. It does not have to pretend to
support writes it will refuse.

The protocol includes no LLM of its own. It governs structure, identity, validation, indices,
snapshots, and distribution; the semantic work is delegated to an interchangeable external model
through :class:`~boltzmann.ingest.proposer.CandidateProposer`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from boltzmann.blocks.base import Block
    from boltzmann.blocks.memory_type import MemoryType
    from boltzmann.distribution.manifest import BrainManifest
    from boltzmann.distribution.registry import RegistryClient
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
        local history. The paper does not define a merge for divergent brains, so the safe behavior is
        to refuse and say where the two parted.

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


@runtime_checkable
class BoltzmannProtocol(BrainReader, BrainWriter, BrainRetention, BrainDistribution, Protocol):
    """An implementation that offers the whole protocol.

    Conforming to this is not required. A read-only client satisfies :class:`BrainReader` and is
    conforming for what it claims to do.
    """
