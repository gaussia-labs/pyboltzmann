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
3. **Authenticity**, implemented, because who assembled a brain must be checkable mechanically:
   detached SSHSIG signatures over snapshots, the trust root that travels inside the brain, scopes
   computed from what a snapshot did, and revocation by chain position rather than by clock. The
   Ed25519 mathematics rides in the optional ``[authenticity]`` extra; everything structural works
   without it, and "could not check" is never reported as either verdict.
4. **The interfaces an implementation satisfies**, declared: ``BrainReader``, ``BrainWriter``,
   ``BrainRetention``, ``BrainDistribution``, ``BrainAuthenticity``, plus ``BlockStore``, ``Index``,
   ``QueryPlanner``, ``Validator``, ``CandidateProposer``, ``RegistryClient``, ``MerkleLayout``,
   ``NormalizationPipeline``.

Ingestion, query, retention, and distribution are **not implemented here**. Where the paper leaves
something to the implementation -- ranking, fusion, index engines, cascade depth, retention
thresholds -- so does this SDK.

It embeds no language model. Interpretation enters through ``CandidateProposer`` and nowhere else.

Reference: *Boltzmann Brain: A Versioned, Distributable, and Model-Agnostic Knowledge Architecture*
(Gaussia, 2026).
"""

from boltzmann.authenticity import (
    AgentSigner,
    AuthenticationReport,
    Authenticator,
    AuthorshipState,
    PinSource,
    RotationPlan,
    RotationResult,
    Scope,
    SignatureRecord,
    Signer,
    SnapshotStance,
    SshPublicKey,
    TrustedKey,
    TrustPin,
    TrustRoot,
    UnsignedPolicy,
    VerificationPolicy,
)
from boltzmann.authenticity.authenticator import Authorship
from boltzmann.blocks import (
    Actor,
    ActorKind,
    Block,
    CanonicalBlock,
    Collaborator,
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
    ProvenanceBlockV2,
    Relation,
    RemovalMechanism,
    SemanticBlock,
    SemanticBlockV2,
    SemanticBlockV3,
    SemanticKind,
    Step,
    require_media_type,
)
from boltzmann.brain import Brain, BrainState
from boltzmann.catalog import (
    Catalog,
    CatalogBrowseResult,
    CatalogDeclaration,
    CatalogDirectory,
    CatalogNode,
    CatalogPathView,
    ClassDeclaration,
    ClassificationRequest,
    HierarchyDeclaration,
    PlacementDeclaration,
    SchemeDeclaration,
)
from boltzmann.catalog_validation import CatalogVerdict, ClassificationResult
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.distribution import FetchResult
from boltzmann.exceptions import BoltzmannError, CatalogError
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
    ValidationAudit,
    ValidationReport,
    ValidationStatus,
    Validator,
)
from boltzmann.merkle import InclusionProof, MerkleLayout, MerkleTree
from boltzmann.module import Composition, Ledger, Module, ModuleRef, Snapshot
from boltzmann.protocol import (
    PROTOCOL_VERSION,
    BoltzmannProtocol,
    BrainAuthenticity,
    BrainDistribution,
    BrainReader,
    BrainReconciliation,
    BrainRetention,
    BrainWriter,
)
from boltzmann.query import EvidenceBundle, Match, Query, QueryFilters, QueryHints, QueryPlanner, RetrievalMode
from boltzmann.reconcile import (
    AttributionReport,
    BlockVerdict,
    IncomingReport,
    MissingEvidence,
    ModuleReconciliation,
    ReconcilePlan,
    ReconcileRequest,
    ReconcileResult,
    ReconcileState,
    ReconcileStatus,
    ReconcileStrategy,
    RemovalAcceptance,
    Resolution,
    ResolutionKind,
)
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

__version__ = "0.9.0-b.3"

__all__ = [
    "Actor",
    "ActorKind",
    "AgentSigner",
    "AttributionReport",
    "AuthenticationReport",
    "Authenticator",
    "Authorship",
    "AuthorshipState",
    "SnapshotStance",
    "Block",
    "BlockId",
    "BlockStore",
    "BlockVerdict",
    "BoltzmannError",
    "BoltzmannProtocol",
    "Brain",
    "BrainAuthenticity",
    "BrainDistribution",
    "BrainReader",
    "BrainReconciliation",
    "BrainRetention",
    "BrainState",
    "BrainWriter",
    "Candidate",
    "CandidateProposer",
    "CandidateSet",
    "CanonicalBlock",
    "Catalog",
    "CatalogBrowseResult",
    "CatalogDeclaration",
    "CatalogDirectory",
    "CatalogError",
    "CatalogNode",
    "CatalogPathView",
    "CatalogVerdict",
    "CascadePlan",
    "ClassDeclaration",
    "ClassificationRequest",
    "ClassificationResult",
    "CommitResult",
    "Composition",
    "ContentReader",
    "ContentRef",
    "DropRequest",
    "DropResult",
    "EpisodicBlock",
    "EpisodicBlockV2",
    "EvidenceBundle",
    "FetchResult",
    "InclusionProof",
    "HierarchyDeclaration",
    "IncomingReport",
    "Index",
    "IndexKind",
    "Ledger",
    "Match",
    "MemoryBlockStore",
    "MemoryType",
    "MerkleLayout",
    "MerkleRoot",
    "MerkleTree",
    "MissingEvidence",
    "Module",
    "ModuleReconciliation",
    "ModuleRef",
    "NamesContent",
    "NormalizedView",
    "OciDigest",
    "OciLayoutStore",
    "PROTOCOL_VERSION",
    "PinSource",
    "PlacementDeclaration",
    "ProceduralBlock",
    "ProceduralBlockV2",
    "ProcessingTask",
    "Producer",
    "ProducerDropRequest",
    "Collaborator",
    "ProvenanceBlock",
    "ProvenanceBlockV2",
    "PruneReport",
    "Query",
    "QueryFilters",
    "QueryHints",
    "QueryPlanner",
    "ReconcilePlan",
    "ReconcileRequest",
    "ReconcileResult",
    "ReconcileState",
    "ReconcileStatus",
    "ReconcileStrategy",
    "RedactionResult",
    "RegistrationRequest",
    "RegistrationResult",
    "Relation",
    "RemovalAcceptance",
    "RemovalMechanism",
    "Resolution",
    "ResolutionKind",
    "ResolvabilityReport",
    "RetentionPolicy",
    "RetrievalMode",
    "RotationPlan",
    "RotationResult",
    "SNAPSHOT_NAMESPACE",
    "Scope",
    "SchemeDeclaration",
    "SemanticBlock",
    "SemanticBlockV2",
    "SemanticBlockV3",
    "SemanticKind",
    "SignatureRecord",
    "Signer",
    "Snapshot",
    "SshPublicKey",
    "Step",
    "SupersessionResult",
    "TrustPin",
    "TrustRoot",
    "TrustedKey",
    "UnsignedPolicy",
    "ValidationAudit",
    "ValidationReport",
    "ValidationStatus",
    "Validator",
    "VerificationPolicy",
    "__version__",
    "require_media_type",
    "utc_timestamp",
]
