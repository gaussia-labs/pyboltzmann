"""The brain manifest: a wire format, so it is implemented rather than left open.

A brain is published as an OCI Artifact referencing one blob per module (paper Section 7.1):

.. code-block:: text

    monotributo-brain:v3
      canonical  -> sha256:AAA
      episodic   -> sha256:BBB
      semantic   -> sha256:CCC
      procedural -> sha256:DDD
      provenance -> sha256:EEE

The manifest is small, and downloading it does not imply downloading all modules. That is what
makes an incremental update cheap: if only the semantic module changed, the new manifest reuses the
digests of the rest, and the consumer downloads one blob (paper Section 7.3).

The manifest is also where the two Merkle DAGs meet. OCI's own structure over manifests,
descriptors, and blobs is an *external* Merkle DAG that versions whole modules; inside each module
layer, an *internal* Merkle DAG versions the individual knowledge blocks. Same idea at two levels
(paper Section 6.2), which is why a descriptor carries both a ``digest`` and a Merkle root
annotation: the first identifies the file, the second the composition inside it.

Transport is not implemented here -- see :class:`~boltzmann.distribution.registry.RegistryClient`
for the interface an implementation provides.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.distribution.media_types import (
    ANNOTATION_BLOCK_COUNT,
    ANNOTATION_EMBEDDING_MODEL,
    ANNOTATION_MEMORY_TYPE,
    ANNOTATION_MERKLE_LAYOUT,
    ANNOTATION_MERKLE_ROOT,
    ANNOTATION_PROTOCOL_VERSION,
    ARTIFACT_TYPE,
    CONFIG_MEDIA_TYPE,
    VECTOR_INDEX_MEDIA_TYPE,
    module_media_type,
)
from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize

if TYPE_CHECKING:
    from boltzmann.module.snapshot import ModuleRef, Snapshot


class Descriptor(BaseModel):
    """
    An OCI descriptor for one layer.

    Attributes:
        media_type (str): The layer's media type.
        digest (OciDigest): Content address of the layer blob -- the physical identity.
        size (int): Size of the layer in bytes.
        annotations (dict[str, str]): Boltzmann annotations, including the layer's internal Merkle
            root, which is the logical identity the digest says nothing about.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    media_type: str = Field(min_length=1)
    digest: OciDigest
    size: int = Field(ge=0)
    annotations: dict[str, str] = Field(default_factory=dict)

    @property
    def memory_type(self) -> MemoryType | None:
        """Which module this layer holds, if it is a module layer."""
        value = self.annotations.get(ANNOTATION_MEMORY_TYPE)
        return MemoryType(value) if value in set(MemoryType) else None

    @property
    def merkle_root(self) -> MerkleRoot | None:
        """The layer's internal Merkle root, if the annotation is present."""
        value = self.annotations.get(ANNOTATION_MERKLE_ROOT)
        return MerkleRoot.parse(value) if value else None

    @property
    def is_vector_index(self) -> bool:
        """Whether this layer is a vector index travelling alongside its module."""
        return self.media_type == VECTOR_INDEX_MEDIA_TYPE

    @classmethod
    def for_module(cls, reference: ModuleRef, digest: OciDigest, size: int) -> Descriptor:
        """
        Build the descriptor for a module layer.

        Args:
            reference (ModuleRef): The module version the layer holds.
            digest (OciDigest): Content address of the layer blob.
            size (int): Size of the layer in bytes.

        Returns:
            Descriptor: The descriptor, annotated so a selective install can resolve it.
        """
        annotations = {
            ANNOTATION_MEMORY_TYPE: reference.memory_type.value,
            ANNOTATION_MERKLE_ROOT: str(reference.root),
            ANNOTATION_MERKLE_LAYOUT: reference.layout,
            ANNOTATION_BLOCK_COUNT: str(reference.block_count),
        }
        if reference.embedding_model is not None:
            annotations[ANNOTATION_EMBEDDING_MODEL] = reference.embedding_model
        return cls(
            media_type=module_media_type(reference.memory_type),
            digest=digest,
            size=size,
            annotations=annotations,
        )


class BrainManifest(BaseModel):
    """
    The published form of a brain.

    Attributes:
        artifact_type (str): Identifies this as a Boltzmann brain.
        config (Descriptor): Points at the snapshot document.
        layers (list[Descriptor]): One per installed module, plus any vector index layers.
        annotations (dict[str, str]): Manifest-level annotations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_type: str = ARTIFACT_TYPE
    config: Descriptor
    layers: list[Descriptor] = Field(default_factory=list)
    annotations: dict[str, str] = Field(default_factory=dict)

    def layer_for(self, memory_type: MemoryType) -> Descriptor | None:
        """
        The layer holding one module.

        This is the lookup a selective install performs: resolve the descriptor marked with the
        wanted memory type, and download only that blob (paper Section 7.2).

        Args:
            memory_type (MemoryType): Which module to find.

        Returns:
            Descriptor | None: The layer, or ``None`` if the module is not in this artifact.
        """
        for layer in self.layers:
            if not layer.is_vector_index and layer.memory_type is memory_type:
                return layer
        return None

    def vector_index_for(self, memory_type: MemoryType) -> Descriptor | None:
        """
        The vector index layer travelling with one module, if any.

        Args:
            memory_type (MemoryType): Which module's index to find.

        Returns:
            Descriptor | None: The index layer, or ``None``.
        """
        for layer in self.layers:
            if layer.is_vector_index and layer.memory_type is memory_type:
                return layer
        return None

    @property
    def modules(self) -> list[MemoryType]:
        """Which modules this artifact carries, in canonical module order."""
        present = {layer.memory_type for layer in self.layers if not layer.is_vector_index}
        return [kind for kind in MemoryType if kind in present]

    def to_bytes(self) -> bytes:
        """
        Serialize the manifest canonically for pushing.

        Returns:
            bytes: The manifest as canonical JSON.
        """
        return canonicalize(self.model_dump(mode="json", exclude_none=True))

    @property
    def digest(self) -> OciDigest:
        """The manifest's own physical identity."""
        return OciDigest.of(self.to_bytes())


def build_manifest(
    snapshot: Snapshot,
    config: Descriptor,
    layers: list[Descriptor],
    annotations: dict[str, str] | None = None,
) -> BrainManifest:
    """
    Assemble a manifest for a snapshot.

    Args:
        snapshot (Snapshot): The state being published. Its modules must all have a layer, or the
            artifact would name a root nobody can fetch.
        config (Descriptor): Descriptor of the snapshot document, which is the config blob.
        layers (list[Descriptor]): The module layers, already pushed.
        annotations (dict[str, str] | None): Extra manifest-level annotations.

    Returns:
        BrainManifest: The manifest to push.

    Raises:
        DistributionError: If the config media type is wrong, or a module named by the snapshot has
            no layer.
    """
    if config.media_type != CONFIG_MEDIA_TYPE:
        raise DistributionError(
            f"config blob must be {CONFIG_MEDIA_TYPE!r}, got {config.media_type!r}: the config of a "
            f"brain artifact is its snapshot document"
        )

    manifest = BrainManifest(
        config=config,
        layers=layers,
        annotations={
            ANNOTATION_PROTOCOL_VERSION: str(PROTOCOL_VERSION),
            **(annotations or {}),
        },
    )

    missing = [kind.value for kind in snapshot.installed if manifest.layer_for(kind) is None]
    if missing:
        raise DistributionError(
            f"the snapshot names roots for {', '.join(missing)} but the artifact carries no layer for "
            f"them, so a consumer could not fetch what the manifest claims"
        )
    return manifest


def parse_manifest(data: bytes) -> BrainManifest:
    """
    Parse a manifest pulled from a registry.

    Args:
        data (bytes): The manifest JSON.

    Returns:
        BrainManifest: The parsed manifest.

    Raises:
        DistributionError: If the artifact is not a Boltzmann brain, or declares a protocol version
            this client does not implement.
    """
    try:
        document: Any = json.loads(data)
    except json.JSONDecodeError as error:
        raise DistributionError(f"manifest is not valid JSON: {error}") from error

    if not isinstance(document, dict):
        raise DistributionError(f"manifest must be an object, got {type(document).__name__}")

    artifact_type = document.get("artifact_type")
    if artifact_type != ARTIFACT_TYPE:
        raise DistributionError(f"not a Boltzmann brain: artifactType is {artifact_type!r}, expected {ARTIFACT_TYPE!r}")

    declared = document.get("annotations", {}).get(ANNOTATION_PROTOCOL_VERSION)
    if declared is not None and declared != str(PROTOCOL_VERSION):
        raise DistributionError(
            f"artifact declares protocol version {declared!r}, this client implements {PROTOCOL_VERSION}"
        )

    return BrainManifest.model_validate(document)
