"""Custom exceptions for Boltzmann.

Every invariant the Boltzmann Protocol declares normative raises a typed error
here rather than returning a status code, so that a violation is a program
failure and not something a caller can quietly ignore.
"""


class BoltzmannError(Exception):
    """Base exception for Boltzmann."""


# --- Identity -----------------------------------------------------------------


class IdentityError(BoltzmannError):
    """Base exception for the three levels of hashes (paper Section 6.4)."""


class DigestFormatError(IdentityError):
    """Exception raised when a digest string is not a valid ``<algorithm>:<hex>``."""


class DigestKindError(IdentityError):
    """Exception raised when one level of hash is used where another is expected.

    A ``BlockId`` identifies knowledge, a ``MerkleRoot`` identifies a logical
    snapshot, and an ``OciDigest`` identifies a transportable blob. They may
    share an algorithm but they never share a meaning.
    """


class SerializationError(IdentityError):
    """Exception raised when a block cannot be canonically serialized."""


class NonDeterministicValueError(SerializationError):
    """Exception raised when a payload holds a value with no canonical form.

    Floats are the motivating case: JCS (RFC 8785) defines their serialization
    through ECMAScript rules that are subtly hard to reproduce across languages,
    so the protocol forbids them outright inside a block payload.
    """


# --- Blocks -------------------------------------------------------------------


class BlockError(BoltzmannError):
    """Base exception for knowledge blocks."""


class BlockSchemaError(BlockError):
    """Exception raised when a block does not satisfy its schema."""


class BlockNotFoundError(BlockError):
    """Exception raised when a block cannot be resolved in a store."""


class BlockIntegrityError(BlockError):
    """Exception raised when stored bytes do not hash to the expected block_id."""


class BlockTombstonedError(BlockError):
    """Exception raised when a block was redacted and its bytes destroyed.

    A conforming implementation must report which blocks of a snapshot are
    resolvable and which are tombstoned, so a removed block is never
    indistinguishable from a corrupted one (paper Section 10.6).
    """


# --- Merkle -------------------------------------------------------------------


class MerkleError(BoltzmannError):
    """Base exception for Merkle DAG operations."""


class InclusionProofError(MerkleError):
    """Exception raised when an inclusion proof does not verify against a root."""


# --- Modules and snapshots ----------------------------------------------------


class ModuleError(BoltzmannError):
    """Base exception for memory modules."""


class MemoryTypeError(ModuleError):
    """Exception raised when a block is used against the wrong memory module."""


class AppendOnlyViolationError(ModuleError):
    """Exception raised when a caller tries to drop from an append-only module.

    The episodic module is a chronological record of what happened, so
    corrections append new episodes or supersession relations rather than
    rewriting the past (paper Section 10.3).
    """


class SnapshotError(ModuleError):
    """Exception raised when a snapshot is inconsistent or unknown."""


class MembershipError(SnapshotError):
    """Exception raised when a block does not belong to the installed snapshot."""


# --- Protocol operations ------------------------------------------------------


class ProtocolError(BoltzmannError):
    """Base exception for Boltzmann Protocol operations."""


class ValidationError(ProtocolError):
    """Exception raised when a candidate block fails validation before commit."""


class CommitError(ProtocolError):
    """Exception raised when a commit cannot be completed atomically."""


class RetentionPolicyError(ProtocolError):
    """Exception raised when a removal is not permitted by the active policy."""


class QueryError(ProtocolError):
    """Exception raised when a query cannot be planned or executed."""


class DistributionError(ProtocolError):
    """Exception raised when publishing to or installing from a registry fails."""


class ReferenceNotFoundError(DistributionError):
    """Exception raised when a registry reports that a reference does not exist.

    Distinct from its parent because "there is nothing published here" and "I could not find out" call for
    opposite responses. Before a first push the absence is expected and a push proceeds; a refused
    credential or a failing registry looks the same to a caller that cannot tell them apart, and a
    fast-forward check that treats every failure as absence stops protecting anything.
    """


class DivergenceError(DistributionError):
    """Exception raised when a remote is not an ancestor of the local snapshot.

    Distinct from its parent because it is the one distribution failure with a defined remedy: the two
    histories advanced from a common ancestor, and Section 12 says what to do about it. A caller that
    can tell this apart from "the registry refused me" can offer to reconcile; one that cannot has to
    treat a resolvable situation as a transport problem.
    """


class ReconciliationError(ProtocolError):
    """Base exception for reconciling two histories (paper Section 12)."""


class NoCommonAncestorError(ReconciliationError):
    """Exception raised when two histories share no ancestor.

    A three-way reconciliation is only defined against a common ancestor: without it, a block present in
    one composition and absent from the other is ambiguous between "they added it" and "I dropped it",
    and those demand opposite outcomes. Section 12.2 requires this to be a distinguishable failure
    rather than a merge computed on a guess.
    """


class ReconciliationHaltedError(ReconciliationError):
    """Exception raised when a reconciliation stopped because something did not apply cleanly.

    Nothing was written and the operation is not lost: what did not apply is recorded, and the reconciliation
    waits to be resolved, concluded, or abandoned. Distinct from its siblings because it is not a failure at
    all -- it is the operation asking a question, and a caller that treats it as an error has nowhere to put
    the answer.
    """


class ReconciliationBlockedError(ReconciliationError):
    """Exception raised when a reconciliation is concluded while a question is still open.

    A conflict here is a validation failure, not a differencing failure, so the verdicts are the report a
    maintainer acts on. Section 12.4 forbids committing while any candidate is still ``PENDING_REVIEW``: the
    protocol declined to decide, and a commit would decide for it.
    """


class ResolutionRefusedError(ReconciliationError):
    """Exception raised when a decision would break an invariant rather than settle a conflict.

    Version control lets an operator force anything into a commit, because what it merges is text and the
    consequences are a human's to judge. Some invariants here are structural: a derived block whose evidence
    is absent from the composition cannot be audited against its source, and nothing downstream would notice
    -- so admitting one is refused, and the refusal names the operation that fixes the cause instead.
    """
