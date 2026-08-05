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
from boltzmann.blocks.content import ContentRef
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
)
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.distribution.layers import pack_module, unpack_layer
from boltzmann.distribution.manifest import BrainManifest, Descriptor, build_manifest, published_artifacts
from boltzmann.distribution.media_types import (
    ANNOTATION_EMBEDDING_MODEL,
    ANNOTATION_INDEX_KIND,
    ANNOTATION_MEMORY_TYPE,
    ANNOTATION_SOURCE_SNAPSHOT,
    ARTIFACT_TYPE,
    CONFIG_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    REF_NAME_ANNOTATION,
    VECTOR_INDEX_MEDIA_TYPE,
)
from boltzmann.distribution.registry import InstallPlan
from boltzmann.exceptions import (
    BlockNotFoundError,
    DistributionError,
    ProtocolError,
    QueryError,
    ReferenceNotFoundError,
    SnapshotError,
)
from boltzmann.identity.digest import BlockId, Digest, MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize
from boltzmann.identity.time import utc_timestamp
from boltzmann.indices.base import TravellingIndex
from boltzmann.ingest.commit import CommitResult
from boltzmann.ingest.pipelines import get_pipeline
from boltzmann.ingest.register import RegistrationRequest, RegistrationResult
from boltzmann.ingest.schema import candidates_schema as _candidates_schema
from boltzmann.ingest.task import PROPOSABLE_MEMORY_TYPES, ProcessingTask, TaskOperation
from boltzmann.ingest.validation import ValidationReport, validate
from boltzmann.module.composition import Composition
from boltzmann.module.ledger import Ledger
from boltzmann.module.module import Module
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.query.scan import scan
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
                if not self.store.is_resolvable(block_id):
                    unreadable = tombstoned if self.store.has(block_id) else missing
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

        Args:
            data (bytes): The content, stored exactly as given.
            media_type (str): IANA media type, recorded in the reference so a consumer can decide
                whether to fetch the bytes without holding them.

        Returns:
            ContentRef: The reference a payload names.
        """
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
        without: dict[MemoryType, list[BlockId]] | None = None,
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

        Returns:
            CommitResult: The new snapshot and the new roots.
        """
        by_module: dict[MemoryType, list[Block]] = {kind: list(items) for kind, items in blocks.items()}
        if provenance:
            by_module.setdefault(MemoryType.PROVENANCE, []).extend(provenance)

        excluded = {kind: list(ids) for kind, ids in (without or {}).items()}
        touched = [*by_module, *(kind for kind in excluded if kind not in by_module)]

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

        blocks = [block_id for block_id in module.block_ids if module.store.is_resolvable(block_id)]
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
            if manifest is None or manifest.config.digest != self._snapshot.digest:
                continue  # Unreadable, or a manifest for some other version of this brain.

            for memory_type in self.indices:
                layer = manifest.vector_index_for(memory_type)
                if layer is None or not self.store.is_resolvable(layer.digest):
                    continue
                try:
                    self._load_index(memory_type, layer)
                except DistributionError:
                    # Most likely an index built by a model this client no longer uses. Refusing it is
                    # right; refusing to *open the brain* over it is not. Opening is not a request to
                    # install anything, so the layer is skipped and the module simply has no vector index
                    # -- which ``travelling_indices`` reports, and a repack replaces.
                    continue
            return

    def _embedding_model(self, memory_type: MemoryType) -> str | None:
        for index in self.indices.get(memory_type, []):
            if index.model_tag is not None:
                return index.model_tag
        reference = self._snapshot.modules.get(memory_type)
        return reference.embedding_model if reference else None

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

        Two limits are worth restating. A hash of low-entropy content is not anonymous, so confirming a
        guess may still be possible while the ``block_id`` is kept. And erasure does not propagate across
        already-pulled copies: a revocation can be published, but a distributed brain can only signal
        destruction, not guarantee it.

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
        commit = self._write(blocks={}, provenance=[record])

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
        for memory_type in published:
            module = self.module(memory_type)
            payload = pack_module(module)
            digest = self.store.put_bytes(payload)
            reference = self._snapshot.modules[memory_type]
            layers.append(Descriptor.for_module(reference, digest, len(payload)))

            index_layer = self._pack_index(memory_type, reference)
            if index_layer is not None:
                layers.append(index_layer)

        projected = self._projection(published)
        config_bytes = projected.canonical_bytes()
        config = Descriptor(
            media_type=CONFIG_MEDIA_TYPE,
            digest=self.store.put_bytes(config_bytes),
            size=len(config_bytes),
        )
        manifest = build_manifest(
            projected,
            config,
            layers,
            annotations={ANNOTATION_SOURCE_SNAPSHOT: str(self._snapshot.digest)},
        )
        manifest_bytes = manifest.to_bytes()
        manifest_digest = self.store.put_bytes(manifest_bytes)
        self._write_index(manifest_digest, len(manifest_bytes), tag)
        return manifest

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

    def _projection(self, published: list[MemoryType]) -> Snapshot:
        """
        The snapshot an artifact carries.

        For a complete publish this is the brain's own snapshot. For a subset it is a projection of it --
        the same roots, fewer modules -- with no parent, because a projection is not a version in this
        brain's history and chaining it would put a document nobody can resolve into the consumer's chain.
        """
        if set(published) == set(self._snapshot.installed):
            return self._snapshot
        return Snapshot(
            boltzmann=self._snapshot.boltzmann,
            modules={kind: self._snapshot.modules[kind] for kind in published},
            created_at=self._snapshot.created_at,
            labels=self._snapshot.labels,
        )

    def _pack_index(self, memory_type: MemoryType, reference: ModuleRef) -> Descriptor | None:
        """A layer for the one index kind a consumer cannot rebuild, or ``None`` if there is none.

        ``None`` also when this brain cannot vouch for the index. An index that was never built here and
        never loaded from a layer holds nothing, and dumping it would publish a layer that claims a vector
        index, carries none, and still says which model produced it -- a consumer loads it, holds nothing,
        and has no way to tell. Omitting the layer is the honest answer: ``plan_pull`` then reports no
        travelling index, which is true.
        """
        travelling = [index for index in self.indices.get(memory_type, []) if not index.rebuildable]
        if not travelling or memory_type not in self._vouched:
            return None

        index = travelling[0]
        if not isinstance(index, TravellingIndex):
            raise DistributionError(
                f"the {index.kind.value} index for {memory_type.value} reports rebuildable=False but "
                f"cannot dump: an index that no client can rebuild has to be publishable, or the module "
                f"arrives without it and nothing can regenerate it"
            )

        payload = index.dump()
        annotations = {
            ANNOTATION_MEMORY_TYPE: memory_type.value,
            ANNOTATION_INDEX_KIND: index.kind.value,
        }
        if index.model_tag is not None:
            annotations[ANNOTATION_EMBEDDING_MODEL] = index.model_tag
        elif reference.embedding_model is not None:
            annotations[ANNOTATION_EMBEDDING_MODEL] = reference.embedding_model

        return Descriptor(
            media_type=VECTOR_INDEX_MEDIA_TYPE,
            digest=self.store.put_bytes(payload),
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

        indices = [
            memory_type
            for memory_type in wanted
            if (layer := manifest.vector_index_for(memory_type)) is not None
            and not self.store.is_resolvable(layer.digest)
        ]

        return InstallPlan(
            modules=wanted,
            fetch_layers=fetch,
            reuse_layers=reuse,
            fetch_vector_indices=indices,
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
        self._require_carried(manifest, wanted)

        if not self.store.is_resolvable(manifest.config.digest):
            await client.pull_blob(reference, manifest.config.digest, self.store)
        remote = Snapshot.model_validate_json(self.store.get_bytes(manifest.config.digest))

        references = []
        for memory_type in wanted:
            layer = manifest.layer_for(memory_type)
            assert layer is not None  # checked above
            if not self.store.is_resolvable(layer.digest):
                await client.pull_blob(reference, layer.digest, self.store)

            # The manifest's layers and its config blob are two separate registry-supplied documents,
            # and nothing forces a registry to keep them consistent. Indexing straight into
            # ``remote.modules`` turned that into a bare KeyError, which is neither documented here nor
            # actionable by a caller.
            expected = remote.modules.get(memory_type)
            if expected is None:
                named = ", ".join(kind.value for kind in remote.installed) or "none"
                raise DistributionError(
                    f"the artifact carries a {memory_type.value} layer but its snapshot names no root for "
                    f"it; the snapshot names: {named}. The manifest and its config disagree, so there is "
                    f"nothing to verify the layer against."
                )

            # Bounded by ``unpack_layer``'s own expansion ratio: the descriptor's size is the compressed
            # blob's, so it says nothing about what decompressing costs.
            composition = unpack_layer(self.store.get_bytes(layer.digest), self.store)
            if composition.root != expected.root:
                raise DistributionError(
                    f"the {memory_type.value} layer unpacks to root {composition.root.short} but the "
                    f"artifact's snapshot names {expected.root.short}"
                )
            references.append(expected)

            # The one derived structure a model-agnostic client cannot rebuild, so it travels.
            index_layer = manifest.vector_index_for(memory_type)
            if index_layer is not None:
                if not self.store.is_resolvable(index_layer.digest):
                    await client.pull_blob(reference, index_layer.digest, self.store)
                self._load_index(memory_type, index_layer)

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
            self._require_not_narrowing(target, target_tag)
            await self._require_fast_forward(client, target, target_tag)

        manifest = self.pack(tag=target_tag, modules=modules)
        digest = await client.push(target, target_tag, manifest, self.store)
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
        except ReferenceNotFoundError:
            return  # Nothing is published here, so there is nothing to overwrite.
        # Any other failure propagates. A guard that cannot read the remote has not checked anything, and
        # one that treats "I could not tell" as "nothing is there" would let an expired credential or a
        # failing registry turn into a push over somebody else's version.

        # A projection's config is not a version in anyone's history, so the manifest records the full
        # snapshot it came from and that is what the ancestry has to contain.
        source = manifest.annotations.get(ANNOTATION_SOURCE_SNAPSHOT)
        remote = OciDigest.parse(source) if source else manifest.config.digest
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
