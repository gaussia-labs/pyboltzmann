"""Package-wide constants.

This module holds only literals and imports nothing, so every layer may depend on it without
inverting the dependency direction of the SDK. The protocol version and the wire schema names live
here rather than under :mod:`boltzmann.protocol` because the kernel needs them -- a block envelope
carries the protocol version -- and the kernel must not depend on the protocol layer.

They are re-exported from :mod:`boltzmann.protocol` for callers, which is where they belong
conceptually.
"""

PROTOCOL_VERSION = 1
"""Version of the Boltzmann Protocol this SDK implements. Stored in every block envelope."""

WIRE_VERSION = 2
"""The highest artifact wire capability this client can consume.

Distinct from :data:`PROTOCOL_VERSION`, which names the document and block schema and
participates in every block identity -- bumping it would change every published ``block_id``
and invalidate the golden vectors. The wire version only gates transfers: an artifact declares
the capability it needs on its manifest (version 2 = the snapshot document carries a
``trust_root``), a client refuses anything above what it implements before moving a single
blob, and nothing content-addressed moves at all.
"""

CANDIDATES_SCHEMA = "boltzmann.candidates/v1"
"""Output schema an external LLM must satisfy when proposing blocks (paper Section 8.2)."""

EVIDENCE_BUNDLE_SCHEMA = "boltzmann.evidence/v1"
"""Schema of the data contract a query returns (paper Section 9.3)."""

PROCESSING_TASK_SCHEMA = "boltzmann.task/v1"
"""Schema of the processing task the protocol hands to an external LLM (paper Section 8.2)."""

SNAPSHOT_NAMESPACE = "boltzmann.snapshot.v1"
"""SSHSIG namespace a snapshot signature is made under (paper Section 8.3).

Not decoration: the namespace is what prevents cross-protocol replay, so a signature a
contributor produced for a Git commit cannot be presented as a Boltzmann one, and a later
version of the protocol can introduce a new namespace without invalidating signatures made
under this one. A conforming implementation MUST reject a signature made under any other.
"""

EMPTY_CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
"""OCI's empty descriptor media type, the config of an artifact that has none of its own."""

EMPTY_CONFIG_BYTES = b"{}"
"""The two bytes of the OCI empty blob."""

EMPTY_CONFIG_DIGEST = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
"""The empty blob's digest, fixed by the OCI spec and pinned here so nobody recomputes it wrong.

These live here rather than in :mod:`boltzmann.distribution.media_types` because the signature
record store needs the digest to keep the blob reachable through a prune, and the kernel must not
depend on the distribution layer.
"""
