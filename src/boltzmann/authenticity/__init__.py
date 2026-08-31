"""Authenticity: who assembled a brain, and why that key should matter (paper Section 8).

Integrity is already total and transitive -- anyone recomputes the hash structure offline and
confirms a brain is exactly what its identifiers describe. What the hashes cannot say is *who
assembled it*: internal consistency is cheap to manufacture, and a fabricated brain recomputes
perfectly. This package carries the one assertion the hashes cannot make -- a detached SSHSIG
signature over the snapshot -- and the machinery for deciding whether the key that made it was
authorized, with the scope the change required, at that position in the chain.

Everything here except the Ed25519 mathematics is standard library. The ``[authenticity]`` extra
buys the verify primitive and nothing else; a reader without it still parses every record,
computes every fingerprint, and rejects a record whose fingerprint and embedded key disagree.
"""

from boltzmann.authenticity.agent import SshAgentClient
from boltzmann.authenticity.backend import signature_backend_available
from boltzmann.authenticity.governance import RotationPlan, RotationResult
from boltzmann.authenticity.keys import (
    ED25519_KEY_TYPE,
    FINGERPRINT_PATTERN,
    SUPPORTED_KEY_TYPES,
    SshPublicKey,
    parse_rfc4253_signature,
    rfc4253_signature,
)
from boltzmann.authenticity.pins import PinSource, TrustPin, read_pin, write_pin
from boltzmann.authenticity.policy import UnsignedPolicy, VerificationPolicy
from boltzmann.authenticity.record import (
    SignatureIndex,
    SignatureRecord,
    for_snapshot,
    reachable_signatures,
    store_record,
)
from boltzmann.authenticity.scopes import PROPOSABLE_SCOPES, Scope
from boltzmann.authenticity.signers import AgentSigner, Signer
from boltzmann.authenticity.sshsig import (
    ARMOR_WRAP,
    DEFAULT_HASH_ALGORITHM,
    HASH_ALGORITHMS,
    MAGIC_PREAMBLE,
    SIG_VERSION,
    SshSignature,
    armor,
    dearmor,
    message_hash,
    normalized,
    sign,
    signed_data,
    verify,
)
from boltzmann.authenticity.trust_root import (
    SinceVerdict,
    TrustedKey,
    TrustRoot,
    confirm_since,
)
from boltzmann.authenticity.wire import MAX_STRING, WireReader, put_string, put_uint32

_SNAPSHOT_FACING = {
    "RequiredScopes": "boltzmann.authenticity.diff",
    "ScopeEvidence": "boltzmann.authenticity.diff",
    "ScopeQuestion": "boltzmann.authenticity.diff",
    "gather_evidence": "boltzmann.authenticity.diff",
    "required_scopes": "boltzmann.authenticity.diff",
    "Position": "boltzmann.authenticity.chain",
    "SnapshotRole": "boltzmann.authenticity.chain",
    "descends_from": "boltzmann.authenticity.chain",
    "locate": "boltzmann.authenticity.chain",
    "observed_revisions": "boltzmann.authenticity.chain",
    "walk_first_parents": "boltzmann.authenticity.chain",
    "AuthenticationReport": "boltzmann.authenticity.authenticator",
    "Authenticator": "boltzmann.authenticity.authenticator",
    "AuthorshipState": "boltzmann.authenticity.authenticator",
    "Finding": "boltzmann.authenticity.authenticator",
    "FindingKind": "boltzmann.authenticity.authenticator",
    "SignatureOutcome": "boltzmann.authenticity.authenticator",
    "SnapshotStance": "boltzmann.authenticity.authenticator",
    "SignatureVerdict": "boltzmann.authenticity.authenticator",
    "RemovalIntegrity": "boltzmann.authenticity.removals",
    "check_removal_invariant": "boltzmann.authenticity.removals",
}
"""Names resolved lazily because their modules read snapshots.

``Snapshot`` carries a ``TrustRoot``, so ``boltzmann.module.snapshot`` initializes this package
mid-import. Anything here that imports the snapshot back must therefore load on first access
rather than eagerly, or the two halves would meet each other half-built. The same pattern
``boltzmann.conformance`` uses for its pytest-backed names, for the same reason: an import that
cannot always run eagerly runs lazily instead of sometimes."""


def __getattr__(name: str):
    if name in _SNAPSHOT_FACING:
        from importlib import import_module

        return getattr(import_module(_SNAPSHOT_FACING[name]), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ARMOR_WRAP",
    "AgentSigner",
    "AuthenticationReport",
    "Authenticator",
    "AuthorshipState",
    "DEFAULT_HASH_ALGORITHM",
    "ED25519_KEY_TYPE",
    "FINGERPRINT_PATTERN",
    "Finding",
    "FindingKind",
    "HASH_ALGORITHMS",
    "MAGIC_PREAMBLE",
    "MAX_STRING",
    "PROPOSABLE_SCOPES",
    "PinSource",
    "Position",
    "RequiredScopes",
    "RemovalIntegrity",
    "SIG_VERSION",
    "SUPPORTED_KEY_TYPES",
    "RotationPlan",
    "RotationResult",
    "Scope",
    "ScopeEvidence",
    "ScopeQuestion",
    "SignatureIndex",
    "SignatureOutcome",
    "SnapshotStance",
    "SignatureRecord",
    "SignatureVerdict",
    "Signer",
    "SinceVerdict",
    "SnapshotRole",
    "SshAgentClient",
    "SshPublicKey",
    "SshSignature",
    "TrustPin",
    "TrustRoot",
    "TrustedKey",
    "UnsignedPolicy",
    "VerificationPolicy",
    "WireReader",
    "armor",
    "check_removal_invariant",
    "confirm_since",
    "dearmor",
    "descends_from",
    "for_snapshot",
    "gather_evidence",
    "locate",
    "message_hash",
    "normalized",
    "observed_revisions",
    "parse_rfc4253_signature",
    "put_string",
    "put_uint32",
    "reachable_signatures",
    "read_pin",
    "required_scopes",
    "rfc4253_signature",
    "sign",
    "signature_backend_available",
    "signed_data",
    "store_record",
    "verify",
    "walk_first_parents",
    "write_pin",
]
