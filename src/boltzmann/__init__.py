"""Boltzmann: an SDK for the Boltzmann Protocol.

The brain conserves, validates, and retrieves knowledge. An external LLM processes, contextualizes,
and uses it.

Knowledge is stored as typed, content-addressed blocks organized into five memory modules --
canonical, episodic, semantic, procedural, and provenance. Each module bundles its blocks, a Merkle
DAG that pins the exact composition of a version, and the indices needed to query it.

**This package is a protocol SDK, not a brain.** It provides three things:

1. **Identity and verification**, implemented, because every conforming client must compute the same
   values: the canonical serialization, ``block_id``, the Merkle root, inclusion proofs.
2. **The types the protocol exchanges**, implemented, because they are wire formats: block schemas,
   ``ProcessingTask``, ``boltzmann.candidates/v1``, ``EvidenceBundle``, ``Query``, the OCI manifest.
3. **The interfaces an implementation satisfies**, declared: ``BrainReader``, ``BrainWriter``,
   ``BrainRetention``, ``BrainDistribution``, plus ``BlockStore``, ``Index``, ``QueryPlanner``,
   ``Validator``, ``CandidateProposer``, ``RegistryClient``, ``MerkleLayout``,
   ``NormalizationPipeline``.

Ingestion, query, retention, and distribution are **not implemented here**. Where the paper leaves
something to the implementation -- ranking, fusion, index engines, cascade depth, retention
thresholds -- so does this SDK.

It embeds no language model. Interpretation enters through ``CandidateProposer`` and nowhere else.

Reference: *Boltzmann Brain: A Versioned, Distributable, and Model-Agnostic Knowledge Architecture*
(Gaussia, 2026).
"""

from boltzmann.blocks import (
    Actor,
    ActorKind,
    Block,
    CanonicalBlock,
    ContentRef,
    EpisodicBlock,
    EpisodicBlockV2,
    MemoryType,
    NamesContent,
    NormalizedView,
    ProceduralBlock,
    ProceduralBlockV2,
    Producer,
    ProvenanceBlock,
    Relation,
    RemovalMechanism,
    SemanticBlock,
    SemanticBlockV2,
    SemanticKind,
    Step,
    require_media_type,
)
from boltzmann.brain import Brain, BrainState
from boltzmann.exceptions import BoltzmannError
from boltzmann.identity import BlockId, MerkleRoot, OciDigest
from boltzmann.identity.time import utc_timestamp
from boltzmann.indices import ContentReader, Index, IndexKind
from boltzmann.ingest import (
    Candidate,
    CandidateProposer,
    CandidateSet,
    CommitResult,
    ProcessingTask,
    RegistrationRequest,
    RegistrationResult,
    ValidationReport,
    ValidationStatus,
    Validator,
)
from boltzmann.merkle import InclusionProof, MerkleLayout, MerkleTree
from boltzmann.module import Composition, Ledger, Module, ModuleRef, Snapshot
from boltzmann.protocol import (
    PROTOCOL_VERSION,
    BoltzmannProtocol,
    BrainDistribution,
    BrainReader,
    BrainRetention,
    BrainWriter,
)
from boltzmann.query import EvidenceBundle, Match, Query, QueryFilters, QueryHints, QueryPlanner, RetrievalMode
from boltzmann.retention import (
    CascadePlan,
    DropRequest,
    DropResult,
    ProducerDropRequest,
    PruneReport,
    RedactionResult,
    ResolvabilityReport,
    RetentionPolicy,
    SupersessionResult,
)
from boltzmann.store import BlockStore, MemoryBlockStore, OciLayoutStore

__version__ = "0.3.0-b.1"

__all__ = [
    "PROTOCOL_VERSION",
    "Actor",
    "ActorKind",
    "Block",
    "BlockId",
    "BlockStore",
    "BoltzmannError",
    "BoltzmannProtocol",
    "Brain",
    "BrainDistribution",
    "BrainReader",
    "BrainRetention",
    "BrainState",
    "BrainWriter",
    "Candidate",
    "CascadePlan",
    "CandidateProposer",
    "CandidateSet",
    "CanonicalBlock",
    "CommitResult",
    "ContentReader",
    "ContentRef",
    "Composition",
    "DropRequest",
    "DropResult",
    "ProducerDropRequest",
    "PruneReport",
    "RedactionResult",
    "ResolvabilityReport",
    "SupersessionResult",
    "EpisodicBlock",
    "EpisodicBlockV2",
    "EvidenceBundle",
    "InclusionProof",
    "Ledger",
    "Index",
    "IndexKind",
    "Match",
    "MemoryBlockStore",
    "MemoryType",
    "MerkleLayout",
    "MerkleRoot",
    "MerkleTree",
    "Module",
    "ModuleRef",
    "NamesContent",
    "NormalizedView",
    "OciDigest",
    "OciLayoutStore",
    "ProceduralBlock",
    "ProceduralBlockV2",
    "ProcessingTask",
    "Producer",
    "ProvenanceBlock",
    "Query",
    "QueryFilters",
    "QueryHints",
    "QueryPlanner",
    "RegistrationRequest",
    "RegistrationResult",
    "Relation",
    "RemovalMechanism",
    "RetentionPolicy",
    "RetrievalMode",
    "SemanticBlock",
    "SemanticBlockV2",
    "SemanticKind",
    "Snapshot",
    "Step",
    "ValidationReport",
    "ValidationStatus",
    "Validator",
    "__version__",
    "require_media_type",
    "utc_timestamp",
]
