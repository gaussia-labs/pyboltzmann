"""OCI media types and annotations: the contract two registries must agree on.

OCI contributes storage, registry authentication, transport, digest-based deduplication, and
tags. Boltzmann contributes the meaning of the modules, the blocks inside them, and their query
contract (paper Section 7.3). These constants are that seam, which is why they are fixed here
rather than left to an implementation: two clients that disagree on the artifact type cannot pull
each other's brains.

**One blob per module is a necessary condition, not an optimization.** Selection is only
efficient if each module is a separate OCI blob: if everything is mixed into a single file, the
whole file must be downloaded (paper Section 7.2).
"""

from __future__ import annotations

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import (  # noqa: F401 - re-exported: this is where transport callers look
    EMPTY_CONFIG_BYTES,
    EMPTY_CONFIG_DIGEST,
    EMPTY_CONFIG_MEDIA_TYPE,
)

ARTIFACT_TYPE = "application/vnd.gaussia.boltzmann.brain.v1+json"
"""``artifactType`` of a published brain manifest."""

CONFIG_MEDIA_TYPE = "application/vnd.gaussia.boltzmann.snapshot.v1+json"
"""Media type of the config blob, which is the snapshot document itself."""

PROJECTION_MEDIA_TYPE = "application/vnd.gaussia.boltzmann.projection.v1+json"
"""Media type of a selective artifact's projection config document."""

MODULE_MEDIA_TYPE_TEMPLATE = "application/vnd.gaussia.boltzmann.module.{memory_type}.v1.tar+gzip"
"""Media type of one module layer. One layer per module keeps selective installation possible.

gzip rather than zstd because it is in the standard library, and a protocol SDK that needed a
compression dependency to read a published brain would be trading portability for a few percent.
Packing is deterministic within this implementation (see :mod:`boltzmann.distribution.layers`), and
an unchanged composition reuses its existing layer digest. Cross-implementation byte identity is not
required: conforming gzip encoders may choose different streams for the same module contents.
"""

VECTOR_INDEX_MEDIA_TYPE = "application/vnd.gaussia.boltzmann.index.vector.v1+bin"
"""Media type of a vector index travelling alongside its module.

The vector index is the one derived structure a model-agnostic client cannot rebuild, so it ships
as its own layer and carries :data:`ANNOTATION_EMBEDDING_MODEL` (paper Section 6.3).
"""

HISTORY_MEDIA_TYPE = "application/vnd.gaussia.boltzmann.history.v1+tar+gzip"
"""Media type of the layer carrying an artifact's snapshot history.

A snapshot names its parents, and an artifact that published only its head would name documents no
consumer can resolve: the chain an audit walks would stop at the first parent, and a contributor's brain
could never be reconciled by anyone but its own publisher, because finding the ancestor two histories
share means reading their parents (paper Sections 12.1 and 12.6).

Its own layer rather than extra blobs, for one reason: a blob no manifest references is unreferenced, and
a registry is entitled to reclaim it. History that a garbage collection can remove is not history.

The cost is small and bounded by what it describes -- a snapshot document is a few hundred bytes and
compresses well against its near-identical siblings, so a history of thousands of versions is a fraction
of any one module layer.
"""

ANNOTATION_SNAPSHOT_COUNT = "ai.gaussia.boltzmann.snapshot-count"
"""How many snapshot documents a history layer carries."""

ANNOTATION_MEMORY_TYPE = "ai.gaussia.boltzmann.memory-type"
"""Which module a layer holds. This is the annotation a selective install resolves on."""

ANNOTATION_MERKLE_ROOT = "ai.gaussia.boltzmann.merkle-root"
"""The layer's internal Merkle root.

Deliberately distinct from the descriptor's own ``digest``: the digest identifies the
transportable file, the root identifies the logical composition inside it. Two registries holding
the same brain agree on digests while knowing nothing about modules or snapshots -- this
annotation is what closes that gap (paper Section 4.3).
"""

ANNOTATION_PROTOCOL_VERSION = "ai.gaussia.boltzmann.protocol-version"
"""Protocol version the artifact conforms to."""

ANNOTATION_SCHEMA_VERSIONS = "ai.gaussia.boltzmann.schema-versions"
"""Which block schema versions each module holds, as ``{"semantic": [1, 2]}``.

A block commits to its schema version inside the envelope that ``block_id`` is computed over, so
the information was always *in* the artifact -- but only reachable by fetching and parsing
envelopes, which is to say after the download a consumer may not be able to use. Declaring it on
the manifest is what lets a client refuse a brain it cannot read before it moves a single byte,
and say why.

Protocol version is a different question and stays a different annotation. That one asks whether
this is a Boltzmann artifact at all; this one asks whether this client has the schemas for the
knowledge inside it. A brain using a schema you lack is still a brain -- your SDK is just too old
to read that module -- so it must be possible to install the modules you *can* read, which a
protocol-level refusal could not express.
"""

ANNOTATION_MERKLE_LAYOUT = "ai.gaussia.boltzmann.merkle-layout"
"""Merkle layout that produced the root. Roots are only comparable between clients that agree here."""

ANNOTATION_BLOCK_COUNT = "ai.gaussia.boltzmann.block-count"
"""How many blocks the module's composition holds."""

ANNOTATION_EMBEDDING_MODEL = "ai.gaussia.boltzmann.embedding-model"
"""Model and version behind a vector index layer, so a consumer can reproduce or compare it."""


def module_media_type(memory_type: MemoryType) -> str:
    """
    Media type of a module layer.

    Args:
        memory_type (MemoryType): Which module the layer holds.

    Returns:
        str: The layer's media type.
    """
    return MODULE_MEDIA_TYPE_TEMPLATE.format(memory_type=memory_type.value)


def memory_type_of(media_type: str) -> MemoryType | None:
    """
    Recover which module a layer media type refers to.

    Args:
        media_type (str): A layer media type.

    Returns:
        MemoryType | None: The module, or ``None`` if the media type is not a module layer.
    """
    for memory_type in MemoryType:
        if media_type == module_media_type(memory_type):
            return memory_type
    return None


MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
"""Media type of the manifest itself, as OCI defines it."""

REF_NAME_ANNOTATION = "org.opencontainers.image.ref.name"
"""Standard annotation that names a manifest in an image index, so a tool can find it by tag."""


ANNOTATION_SOURCE_SNAPSHOT = "ai.gaussia.boltzmann.source-snapshot"
"""The publisher's full snapshot an artifact was projected from.

An unauthenticated pre-download hint. For a complete publish it equals the config digest; for a
projection the authoritative binding is the ``source`` digest inside the projection document.
"""


ANNOTATION_INDEX_KIND = "ai.gaussia.boltzmann.index-kind"
"""Which kind of index a travelling index layer carries, so a consumer knows what it received."""


SIGNATURE_MEDIA_TYPE = "application/vnd.gaussia.boltzmann.signature.v1+json"
"""Media type of a signature record blob, and the ``artifactType`` of the manifest that carries it.

A signature is never a layer of the brain manifest: adding a countersignature would change the
manifest, and therefore the brain's digest, so a brain would change identity because someone
agreed with it. It is the single layer of its **own** manifest, whose ``subject`` names the brain
-- the OCI Referrers arrangement, which is how signature attestations already accumulate around
artifacts without touching them (paper Section 8.8). Do not "fix" this back into a layer.
"""

# The OCI empty-blob literals live in :mod:`boltzmann.constants` -- the signature record store
# also needs them, and the kernel must not depend on this layer -- and are re-exported here,
# which is where a distribution caller expects to find them.

ANNOTATION_TRUST_ROOT = "ai.gaussia.boltzmann.trust-root"
"""Digest of the trust root carried inside the config blob.

The information was always in the artifact -- reachable only by fetching the config -- and
declaring it on the manifest is what lets a consumer compare it against a pin *before*
transferring anything (paper Section 8.8), exactly as ANNOTATION_SCHEMA_VERSIONS lets a client
refuse a brain it cannot read before it moves a byte.
"""

ANNOTATION_SIGNATURE_KEY = "ai.gaussia.boltzmann.signature-key"
"""Fingerprint of the key behind a signature manifest, so registry tooling can list signers
without fetching a blob. An index for humans and dashboards; verification never reads it."""

ANNOTATION_SIGNATURE_SNAPSHOT = "ai.gaussia.boltzmann.signature-snapshot"
"""The snapshot digest a signature manifest's record covers, for the same browsing purpose."""

REFERRERS_TAG_PREFIX = "sha256-"
"""Tag scheme of the referrers fallback: an index at ``sha256-<hex>`` lists the referrers of the
manifest with that digest, for registries that predate the Referrers API. Registry support for
the API has been uneven, and a signature that cannot be published is a feature that does not
exist -- so the fallback is implemented, not merely tolerated."""

IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
"""Media type of an OCI image index, which is what the referrers API and its fallback both return."""
