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

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.canonical import CanonicalBlock, NormalizedView
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    DerivationRecord,
    NormalizationRecord,
    Producer,
    ProducerKind,
    ProvenanceBlock,
    RegistrationRecord,
    SupersessionRecord,
)
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.distribution.layers import pack_module, unpack_layer
from boltzmann.distribution.manifest import BrainManifest, Descriptor, build_manifest
from boltzmann.distribution.media_types import (
    ARTIFACT_TYPE,
    CONFIG_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    REF_NAME_ANNOTATION,
)
from boltzmann.exceptions import (
    BlockNotFoundError,
    DistributionError,
    ProtocolError,
    QueryError,
    SnapshotError,
)
from boltzmann.identity.digest import BlockId, MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize
from boltzmann.identity.time import utc_timestamp
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.pipelines import get_pipeline
from boltzmann.ingest.register import RegistrationRequest, RegistrationResult
from boltzmann.ingest.schema import candidates_schema as _candidates_schema
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES, ProcessingTask, TaskOperation
from boltzmann.ingest.validation import ValidationReport, validate
from boltzmann.module.composition import Composition
from boltzmann.module.module import Module
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.query.scan import scan
from boltzmann.retention.policy import RetentionPolicy
from boltzmann.retention.requests import ResolvabilityReport
from boltzmann.store.oci_layout import OciLayoutStore

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from boltzmann.blocks.base import Block
    from boltzmann.distribution.registry import RegistryClient
    from boltzmann.indices.base import Index, IndexKind
    from boltzmann.ingest.proposer import CandidateProposer, CandidateSet
    from boltzmann.ingest.validation import Validator
    from boltzmann.merkle.proof import InclusionProof
    from boltzmann.query.evidence import EvidenceBundle
    from boltzmann.query.planner import QueryPlanner
    from boltzmann.query.request import Query
    from boltzmann.store.base import BlockStore

HEAD_POINTER = "head"
"""Name of the mutable pointer that says which snapshot is current."""


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

    def __repr__(self) -> str:
        installed = ", ".join(kind.value for kind in self._snapshot.installed) or "empty"
        return f"Brain({installed}, blocks={self._snapshot.block_count})"

    # --- State ----------------------------------------------------------------

    def _read_state(self) -> BrainState | None:
        raw = self.store.read_pointer(HEAD_POINTER)
        return BrainState.model_validate_json(raw) if raw else None

    def _load_snapshot(self) -> Snapshot:
        if self._state is None:
            return Snapshot()
        return Snapshot.model_validate_json(self.store.get_bytes(self._state.snapshot))

    def _advance(self, snapshot: Snapshot, origin: Origin | None = None) -> Snapshot:
        """Write the snapshot document, then move the pointer. Order matters for atomicity."""
        digest = self.store.put_bytes(snapshot.canonical_bytes())
        retained = [digest, *(self._state.retained if self._state else [])]
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

    @property
    def origin(self) -> Origin | None:
        """Where this brain was pulled from, if it was."""
        return self._state.origin if self._state else None

    def ancestry(self) -> list[OciDigest]:
        """
        The snapshot digests reachable from the current one by walking ``parent``.

        This is what a fast-forward check compares against: a push is safe when the remote's snapshot
        appears here, because that means the local history contains the remote's.

        Returns:
            list[OciDigest]: The current snapshot first, then each ancestor still resolvable.
        """
        if self._state is None:
            return []
        chain = [self._state.snapshot]
        snapshot: Snapshot | None = self._snapshot
        while snapshot is not None and snapshot.parent is not None:
            parent = snapshot.parent
            chain.append(parent)
            if not self.store.is_resolvable(parent):
                break
            snapshot = Snapshot.model_validate_json(self.store.get_bytes(parent))
        return chain

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
        module = Module(memory_type, self.store, composition, self._index_map(memory_type))
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

    def resolvability(self) -> ResolvabilityReport:
        """
        Report which blocks resolve, which were tombstoned, and which are simply missing.

        The three-way split is required, not cosmetic: a redacted block and a corrupted one both fail to
        read, and a consumer that cannot tell them apart cannot tell a lawful erasure from a broken
        store (paper Section 10.6).

        Returns:
            ResolvabilityReport: The classification, per module.
        """
        resolvable: dict[MemoryType, list[BlockId]] = {}
        tombstoned: dict[MemoryType, list[BlockId]] = {}
        missing: dict[MemoryType, list[BlockId]] = {}

        for memory_type in self._snapshot.installed:
            module = self.module(memory_type)
            for block_id in module.block_ids:
                if self.store.is_resolvable(block_id):
                    resolvable.setdefault(memory_type, []).append(block_id)
                elif self.store.has(block_id):
                    tombstoned.setdefault(memory_type, []).append(block_id)
                else:
                    missing.setdefault(memory_type, []).append(block_id)

        return ResolvabilityReport(resolvable=resolvable, tombstoned=tombstoned, missing=missing)

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
        if self.planner is not None:
            return self.planner.plan(query, modules)
        return scan(query, modules)

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
    ) -> CommitResult:
        """
        Store blocks, advance the affected compositions, and publish a snapshot.

        Every mutation in this class funnels through here, which is what makes "the LLM never writes
        directly to the Merkle DAGs or to the indices" a property of the code: there is one place that
        writes, and it is reached only after validation.

        Args:
            blocks (dict[MemoryType, list[Block]]): Blocks to add, by module.
            provenance (Sequence[ProvenanceBlock]): Provenance entries to record alongside them.

        Returns:
            CommitResult: The new snapshot and the new roots.
        """
        by_module: dict[MemoryType, list[Block]] = {kind: list(items) for kind, items in blocks.items()}
        if provenance:
            by_module.setdefault(MemoryType.PROVENANCE, []).extend(provenance)

        committed: list[BlockId] = []
        references: list[ModuleRef] = []
        roots: dict[MemoryType, MerkleRoot] = {}

        for memory_type, items in by_module.items():
            for block in items:
                self.store.put_block(block)

            module = self._module_or_empty(memory_type).with_blocks(block.block_id for block in items)
            self._rebuild_indices(module)
            reference = module.persist(embedding_model=self._embedding_model(memory_type))
            references.append(reference)
            roots[memory_type] = reference.root
            if memory_type is not MemoryType.PROVENANCE:
                committed.extend(block.block_id for block in items)

        # One commit is one version, however many modules it advanced. A brain's first version has no
        # parent: the empty snapshot a fresh handle starts from is a placeholder, never a published
        # document, so chaining to it would put an unresolvable digest in every ancestry.
        if self._state is None:
            snapshot = Snapshot(modules={ref.memory_type: ref for ref in references})
        else:
            snapshot = self._snapshot.with_modules(references)
        self._advance(snapshot)
        return CommitResult(
            snapshot=snapshot,
            committed=committed,
            provenance=[block.block_id for block in provenance],
            roots=roots,
        )

    def _rebuild_indices(self, module: Module) -> None:
        """Indices are derived, so they are rebuilt from the composition rather than patched."""
        for index in self.indices.get(module.memory_type, []):
            index.build(module.blocks())

    def _embedding_model(self, memory_type: MemoryType) -> str | None:
        for index in self.indices.get(memory_type, []):
            if index.model_tag is not None:
                return index.model_tag
        reference = self._snapshot.modules.get(memory_type)
        return reference.embedding_model if reference else None

    # --- Distribution ----------------------------------------------------------

    def pack(self, tag: str | None = None) -> BrainManifest:
        """
        Materialize the current snapshot as an OCI artifact inside the local layout.

        One layer per module, the snapshot as the config blob, and the manifest written into
        ``index.json``. After this the directory is not merely an OCI *layout* -- it carries an
        *artifact*, so ``oras`` or any OCI tool can copy it without this SDK being involved.

        This is what makes publishing a transfer rather than a conversion: :meth:`push` packs and then
        moves blobs that already exist.

        Args:
            tag (str | None): Reference name to record in the index, so a tool can find the artifact
                by name rather than by digest.

        Returns:
            BrainManifest: The manifest, already stored as a blob.
        """
        layers = []
        for memory_type in self._snapshot.installed:
            module = self.module(memory_type)
            payload = pack_module(module)
            digest = self.store.put_bytes(payload)
            layers.append(Descriptor.for_module(self._snapshot.modules[memory_type], digest, len(payload)))

        config_bytes = self._snapshot.canonical_bytes()
        config = Descriptor(
            media_type=CONFIG_MEDIA_TYPE,
            digest=self.store.put_bytes(config_bytes),
            size=len(config_bytes),
        )
        manifest = build_manifest(self._snapshot, config, layers)
        manifest_bytes = manifest.to_bytes()
        manifest_digest = self.store.put_bytes(manifest_bytes)
        self._write_index(manifest_digest, len(manifest_bytes), tag)
        return manifest

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

    async def pull(
        self,
        client: RegistryClient,
        reference: str,
        tag: str,
        modules: Iterable[MemoryType] | None = None,
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

        Returns:
            Snapshot: The newly installed state.

        Raises:
            DistributionError: If a wanted module is not in the artifact, or a layer does not verify.
        """
        manifest = await client.resolve(reference, tag)
        wanted = list(modules) if modules is not None else manifest.modules

        missing = [kind.value for kind in wanted if manifest.layer_for(kind) is None]
        if missing:
            carried = ", ".join(kind.value for kind in manifest.modules) or "none"
            raise DistributionError(f"the artifact does not carry {', '.join(missing)}; it carries: {carried}")

        if not self.store.is_resolvable(manifest.config.digest):
            await client.pull_blob(reference, manifest.config.digest, self.store)
        remote = Snapshot.model_validate_json(self.store.get_bytes(manifest.config.digest))

        references = []
        for memory_type in wanted:
            layer = manifest.layer_for(memory_type)
            assert layer is not None  # checked above
            if not self.store.is_resolvable(layer.digest):
                await client.pull_blob(reference, layer.digest, self.store)

            composition = unpack_layer(self.store.get_bytes(layer.digest), self.store)
            expected = remote.modules[memory_type]
            if composition.root != expected.root:
                raise DistributionError(
                    f"the {memory_type.value} layer unpacks to root {composition.root.short} but the "
                    f"artifact's snapshot names {expected.root.short}"
                )
            references.append(expected)

        complete = set(wanted) == set(manifest.modules)
        if complete:
            # Adopt the remote document verbatim. Rebuilding an equivalent one would give it a fresh
            # ``created_at`` and therefore a different digest, and the fast-forward check compares
            # digests -- so a push back to the same tag would look like a divergence when nothing
            # diverged at all.
            installed = remote
        else:
            installed = Snapshot(
                boltzmann=remote.boltzmann,
                modules={reference_.memory_type: reference_ for reference_ in references},
                labels=remote.labels,
            )

        origin = Origin(
            reference=reference,
            tag=tag,
            snapshot=manifest.config.digest,
            partial=not complete,
        )
        return self._advance(installed, origin=origin)

    async def push(
        self,
        client: RegistryClient,
        reference: str | None = None,
        tag: str | None = None,
        force: bool = False,
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
            self._require_not_narrowing(target, target_tag)
            await self._require_fast_forward(client, target, target_tag)

        manifest = self.pack(tag=target_tag)
        digest = await client.push(target, target_tag, manifest, self.store)
        self._advance(
            self._snapshot,
            origin=Origin(reference=target, tag=target_tag, snapshot=self._state.snapshot),
        )
        return digest

    def _push_target(self, reference: str | None, tag: str | None) -> tuple[str, str]:
        origin = self.origin
        target = reference or (origin.reference if origin else None)
        target_tag = tag or (origin.tag if origin else None)
        if target is None or target_tag is None:
            raise DistributionError(
                "no repository to push to: this brain was not pulled from one, so pass a reference and a tag"
            )
        return target, target_tag

    def _require_not_narrowing(self, reference: str, tag: str) -> None:
        """Refuse to republish a partial install over the tag it came from.

        The modules that were never fetched would silently disappear from the artifact. Publishing the
        same partial brain somewhere *else* is legitimate -- a semantic-only brain is a valid artifact --
        so only the same reference and tag are refused.
        """
        origin = self.origin
        if origin is None or not origin.partial:
            return
        if (reference, tag) == (origin.reference, origin.tag):
            installed = ", ".join(kind.value for kind in self._snapshot.installed)
            raise DistributionError(
                f"this brain was installed partially ({installed}) from {reference}:{tag}; republishing it "
                f"there would drop the modules that were never fetched. Push to a different tag, pull the "
                f"rest first, or pass force=True."
            )

    async def _require_fast_forward(self, client: RegistryClient, reference: str, tag: str) -> None:
        """Refuse a push that would drop a remote snapshot this brain does not contain."""
        try:
            manifest = await client.resolve(reference, tag)
        except DistributionError:
            return  # The tag does not exist yet, so there is nothing to overwrite.

        remote = manifest.config.digest
        ancestry = self.ancestry()
        if remote in ancestry:
            return

        raise DistributionError(
            f"{reference}:{tag} is at snapshot {remote.short}, which is not in this brain's history; "
            f"the two diverged. Pull and re-commit, or pass force=True to overwrite the remote."
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
                snapshots.append(Snapshot.model_validate_json(self.store.get_bytes(digest)))
        return snapshots

    def state(self) -> dict[str, Any]:
        """
        The brain's mutable pointer, for tooling that inspects a layout.

        Returns:
            dict[str, Any]: The head pointer as stored, or an empty mapping for a fresh brain.
        """
        return json.loads(self._state.model_dump_json()) if self._state else {}
