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


class CatalogError(ProtocolError):
    """Exception raised when catalog structure or navigation is invalid."""


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


class MultipleMergeBasesError(ReconciliationError):
    """Exception raised when reconciliation has more than one best common ancestor.

    A criss-cross history can have several incomparable common ancestors. Choosing whichever one a
    traversal happens to encounter first makes the three-way merge depend on parent order, so the
    protocol requires the histories to be reconciled until a later merge has one unambiguous base.
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


class GovernanceConflictError(ReconciliationError):
    """Exception raised when the histories being reconciled carry different trust roots.

    The one conflict reconciliation must refuse outright rather than surface as a candidate:
    unioning two key lists grants the union of both sides' permissions, which defeats the quorum
    rule (paper Section 8.5). The reconciliation is refused and the change of authority is
    resolved as an explicit governance act -- a trust-root revision -- never as a merge.
    """


# --- Authenticity ---------------------------------------------------------------


class AuthenticityError(ProtocolError):
    """Base exception for authorship and trust (paper Section 8).

    Deliberately separate from every integrity failure: integrity is recomputed from bytes and
    needs no configuration, while authenticity is judged against a trust root. A caller that
    cannot tell the two apart reports "corrupt" where it means "unauthorized", and the paper
    forbids collapsing them into one verdict.
    """


class SignatureFormatError(AuthenticityError):
    """Exception raised when an armored signature, an SSHSIG blob, or SSH wire framing cannot be read.

    Distinct from :class:`SignatureInvalidError` because the two call for opposite responses: a
    malformed record is a parse failure its producer can fix, while an invalid signature is a
    claim that did not hold. Reporting a truncated blob as a forgery accuses the wrong party.
    """


class SignatureInvalidError(AuthenticityError):
    """Exception raised when a signature does not verify over the bytes it claims to cover.

    The one authenticity failure that means forgery or corruption: the mathematics failed. Every
    sibling here describes a signature that is real but inapplicable -- wrong namespace, wrong
    key, wrong position -- and folding them into this class would turn routine administrative
    facts into accusations.
    """


class NamespaceMismatchError(AuthenticityError):
    """Exception raised when a signature was made under a namespace other than the protocol's.

    The namespace is what stops cross-protocol replay: a signature a contributor produced for a
    Git commit must not be presentable as a Boltzmann one (paper Section 8.3). Distinct from
    :class:`SignatureInvalidError` because this signature is genuine -- it was simply made for
    something else, and telling a contributor their key is broken when they signed a Git commit
    is a false diagnosis.
    """


class KeyMismatchError(AuthenticityError):
    """Exception raised when a record's named fingerprint and its embedded public key disagree.

    SSHSIG carries the public key inside the signature blob, so the record's ``key`` field is an
    index rather than an authority, and a record whose two identities disagree must be rejected
    (paper Section 8.3). Distinct because it is the one rejection that needs no cryptography at
    all: it names a record that is internally inconsistent, not one that is merely unauthorized,
    and it must therefore keep working on an install without the ``[authenticity]`` extra.
    """


class UnsupportedKeyTypeError(AuthenticityError):
    """Exception raised when a key or signature uses an algorithm this client does not implement.

    A well-formed signature this client cannot check is not an invalid one: the brain may be
    perfectly signed and this SDK simply too narrow. Reporting it as
    :class:`SignatureInvalidError` would reject a legitimately signed brain as forged, which is
    a worse failure than naming the limit.
    """


class WeakKeyError(AuthenticityError):
    """Exception raised when a key is below the protocol's verification security floor.

    Distinct from :class:`UnsupportedKeyTypeError`: an unsupported key may become verifiable in
    a wider client, while DSA and undersized RSA are deliberately refused even by a client that
    otherwise implements them. Callers can therefore distinguish missing capability from a
    security-policy rejection without treating either one as a forged signature.
    """


class UnsignedBrainError(AuthenticityError):
    """Exception raised when a signature was required and none is present.

    Distinct because absence and failure demand different responses: an unsigned brain is the
    zero-configuration case the protocol explicitly permits (paper Section 8.1), and whether it
    is acceptable is the verification policy's decision -- not a fact about any signature.
    """


class UnauthorizedKeyError(AuthenticityError):
    """Exception raised when the signing key is absent from the trust root in force at a position.

    The signature is genuine; the authority is not. This is the failure that defeats the
    self-admission attack (paper Section 8.9, Case 2): an attacker can write any key list they
    like, but the trust root in force at their snapshot is the one its parent names, and their
    key is not in it.
    """


class InsufficientScopeError(AuthenticityError):
    """Exception raised when a key is authorized, but not for the change its snapshot made.

    The required scope set is computed from the snapshot's difference against its first parent,
    never taken from what the signature claims (paper Section 8.5). Distinct from
    :class:`UnauthorizedKeyError` because the remedies differ: an unlisted key needs admission,
    a listed one needs a scope grant -- and the second is a far smaller governance act.
    """


class RetiredKeyError(AuthenticityError):
    """Exception raised when a key was authorized once and is not at this position.

    Signatures the key made at earlier positions remain valid: retirement can never invalidate a
    signature that was valid before it, which is what makes an ordinary departure harmless
    (paper Section 8.6). A caller that cannot tell this from :class:`UnauthorizedKeyError` will
    invalidate stretches of history for an administrative reason.
    """


class CompromisedKeyError(AuthenticityError):
    """Exception raised when a key's signatures are withdrawn from a recorded chain position onward.

    The only construct in the protocol that invalidates a previously valid signature, and the
    paper requires it reported as such rather than as an ordinary authorization failure (paper
    Section 8.6): "signed by a retired key, still valid" and "signed by a compromised key, no
    longer trusted" are different facts about the same snapshot, and collapsing them either
    breaks history or hides an attack.
    """


class QuorumFailureError(AuthenticityError):
    """Exception raised when a trust-root revision lacks the required ``govern`` signatures.

    A revision must carry at least ``govern_quorum`` valid signatures from distinct keys holding
    ``govern`` in the trust root as it stood *before* the change (paper Section 8.5). Evaluating
    against the previous revision is the half that is easy to lose -- and the half that stops
    the key list from authorizing itself.
    """


class TrustRootMismatchError(AuthenticityError):
    """Exception raised when a brain's trust root does not match the digest a consumer pinned.

    The pin lives in consumer-side state and never in the artifact -- a pin the artifact could
    supply would be a pin the attacker supplies (paper Section 8.8). Distinct because it is the
    one failure that compares against something outside the brain, so it can reject an artifact
    whose every internal check passes.
    """


class VerificationUnavailableError(AuthenticityError):
    """Exception raised when checking a signature needs the ``[authenticity]`` extra.

    "I could not check" is a different fact from "it failed", and a caller that cannot tell them
    apart treats a missing dependency as a forgery -- or worse, an attacker's uninstall becomes
    one. Every structural check (framing, fingerprints, authorization, quorum arithmetic) works
    without the extra; only the Ed25519 mathematics raises this.
    """


class SignerUnavailableError(AuthenticityError):
    """Exception raised when no signing backend can produce a signature.

    A signing-side failure that says nothing about any signature: no agent is listening on
    ``SSH_AUTH_SOCK``, the agent refused, or it does not hold the requested key. The private key
    never enters the protocol (paper Section 8.3), so the SDK cannot fall back to reading one.
    """
