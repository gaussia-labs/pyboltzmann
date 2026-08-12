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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.distribution.media_types import (
    ANNOTATION_BLOCK_COUNT,
    ANNOTATION_EMBEDDING_MODEL,
    ANNOTATION_MEMORY_TYPE,
    ANNOTATION_MERKLE_LAYOUT,
    ANNOTATION_MERKLE_ROOT,
    ANNOTATION_PROTOCOL_VERSION,
    ANNOTATION_SCHEMA_VERSIONS,
    ARTIFACT_TYPE,
    CONFIG_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    REF_NAME_ANNOTATION,
    VECTOR_INDEX_MEDIA_TYPE,
    module_media_type,
)
from boltzmann.exceptions import BlockNotFoundError, DistributionError, IdentityError
from boltzmann.identity.digest import MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from boltzmann.module.snapshot import ModuleRef, Snapshot
    from boltzmann.store.base import BlockStore


OCI_SCHEMA_VERSION = 2
"""What the OCI image-manifest specification fixes this at. Present in the document because the spec
requires it, and because its absence is how a consumer's parser learns the document is not a manifest."""


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

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    media_type: str = Field(
        min_length=1,
        validation_alias=AliasChoices("mediaType", "media_type"),
        serialization_alias="mediaType",
    )
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
        schema_version (int): OCI's manifest schema version, which the spec fixes at 2.
        media_type (str): This document's own media type, ``application/vnd.oci.image.manifest.v1+json``.
        artifact_type (str): Identifies this as a Boltzmann brain.
        config (Descriptor): Points at the snapshot document.
        layers (list[Descriptor]): One per installed module, plus any vector index layers.
        annotations (dict[str, str]): Manifest-level annotations.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # Validation accepts both spellings and serialization emits only OCI's. The snake_case alternatives
    # exist because manifests written by an earlier version of this SDK are sitting in real layouts, and
    # refusing to read one would strand a brain to no purpose.
    schema_version: int = Field(
        default=OCI_SCHEMA_VERSION,
        validation_alias=AliasChoices("schemaVersion", "schema_version"),
        serialization_alias="schemaVersion",
    )
    media_type: str = Field(
        default=MANIFEST_MEDIA_TYPE,
        validation_alias=AliasChoices("mediaType", "media_type"),
        serialization_alias="mediaType",
    )
    artifact_type: str = Field(
        default=ARTIFACT_TYPE,
        validation_alias=AliasChoices("artifactType", "artifact_type"),
        serialization_alias="artifactType",
    )
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
        Serialize the manifest as an OCI image manifest.

        **These are the bytes everywhere.** The local layout stores them, a registry receives them
        unaltered, and both therefore file the artifact under the same digest -- which is what makes the
        paper's claim true rather than aspirational: the on-disk brain *is* an OCI artifact, so publishing
        is a copy and not a conversion (Section 7). Serializing a private shape locally and translating it
        on the way out would give one brain two names and leave a directory that no OCI tool can read.

        Empty annotation maps are dropped. They carry nothing, and omitting them keeps the bytes identical
        to what a conventional OCI producer would write for the same content.

        Returns:
            bytes: The manifest as canonical JSON, in OCI's wire shape.
        """
        document = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        for descriptor in [document["config"], *document.get("layers", [])]:
            if not descriptor.get("annotations"):
                descriptor.pop("annotations", None)
        if not document.get("annotations"):
            document.pop("annotations", None)
        return canonicalize(document)

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
        # Protocol-owned keys are written last. Splatting the caller's annotations after them let a
        # caller overwrite the protocol version -- the one annotation a consumer refuses on -- so the
        # check could be disabled by the side that benefits from disabling it.
        annotations={
            **(annotations or {}),
            ANNOTATION_PROTOCOL_VERSION: str(PROTOCOL_VERSION),
        },
    )

    missing = [kind.value for kind in snapshot.installed if manifest.layer_for(kind) is None]
    if missing:
        raise DistributionError(
            f"the snapshot names roots for {', '.join(missing)} but the artifact carries no layer for "
            f"them, so a consumer could not fetch what the manifest claims"
        )
    return manifest


def declare_schema_versions(versions: Mapping[MemoryType, Sequence[int]]) -> str:
    """
    Encode a module-to-schema-versions map for :data:`ANNOTATION_SCHEMA_VERSIONS`.

    Canonically serialized rather than merely dumped, because the annotation travels inside the
    manifest and the manifest's digest is what push deduplication and the fast-forward check
    compare. Two clients publishing the same brain have to produce the same bytes.

    Args:
        versions (Mapping[MemoryType, Sequence[int]]): Versions present in each module. A module
            with no versions -- an empty composition -- is omitted rather than declared empty.

    Returns:
        str: The annotation value.
    """
    declared = {kind.value: sorted(set(present)) for kind, present in versions.items() if present}
    return canonicalize(declared).decode()


def schema_versions_of(manifest: BrainManifest) -> dict[MemoryType, tuple[int, ...]]:
    """
    Which block schema versions each module of an artifact holds, as the manifest declares them.

    An artifact published before this annotation existed carries no declaration, and an empty
    result means exactly that: *unknown*, not *none*. A consumer cannot distinguish "this brain
    needs nothing special" from "this publisher was too old to say", so absence must fall through
    to the decode-time check rather than be read as permission.

    Args:
        manifest (BrainManifest): The manifest to read.

    Returns:
        dict[MemoryType, tuple[int, ...]]: Versions per module, empty when undeclared or
        unparseable. Unknown memory types are skipped: a module this client has no concept of is
        not one it can be asked to install.
    """
    declared = manifest.annotations.get(ANNOTATION_SCHEMA_VERSIONS)
    if not declared:
        return {}

    try:
        parsed = json.loads(declared)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    versions: dict[MemoryType, tuple[int, ...]] = {}
    for name, present in parsed.items():
        try:
            memory_type = MemoryType(name)
        except ValueError:
            continue
        # Registry-supplied, so the types are untrusted too, not just the values.
        if isinstance(present, list) and all(
            isinstance(version, int) and not isinstance(version, bool) for version in present
        ):
            versions[memory_type] = tuple(sorted(set(present)))
    return versions


def require_supported_schemas(manifest: BrainManifest, wanted: Iterable[MemoryType]) -> None:
    """
    Refuse an artifact whose wanted modules use a block schema this client does not implement.

    Scoped to the modules actually being installed. A brain whose semantic module uses a newer
    schema is still perfectly installable if what you asked for is the episodic one, and refusing
    the whole artifact would deny a consumer knowledge it can read to protect it from knowledge it
    never requested.

    Args:
        manifest (BrainManifest): The artifact's manifest.
        wanted (Iterable[MemoryType]): The modules about to be installed.

    Raises:
        DistributionError: If a wanted module declares a schema version with no registered class.
            Raised before any layer is fetched, so nothing is downloaded and nothing is written.
    """
    declared = schema_versions_of(manifest)
    if not declared:
        return

    registry = Block.registry()
    for memory_type in wanted:
        known = sorted(version for kind, version in registry if kind is memory_type)
        unsupported = [version for version in declared.get(memory_type, ()) if version not in known]
        if unsupported:
            raise DistributionError(
                f"the {memory_type.value} module holds blocks with schema "
                f"{'versions' if len(unsupported) > 1 else 'version'} "
                f"{', '.join(str(version) for version in unsupported)}; this client implements {known}. "
                f"The artifact was published by a newer SDK -- upgrade boltzmann to one that implements "
                f"schema version {max(unsupported)} for {memory_type.value} blocks, or install only the "
                f"modules this client has schemas for"
            )


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    """
    One entry of a layout's ``index.json``, resolved as far as it can be.

    Attributes:
        digest (OciDigest): The manifest's own digest, as the index names it.
        tag (str | None): The tag it is published under, if it carries one.
        manifest (BrainManifest | None): The parsed manifest, or ``None`` when the bytes are absent or this
            client cannot read them. Absent is not the same as unreadable, and neither is the same as
            missing from the index, so a caller decides what to do rather than being told nothing.
    """

    digest: OciDigest
    tag: str | None
    manifest: BrainManifest | None


def published_artifacts(store: BlockStore) -> list[PublishedArtifact]:
    """
    Every artifact the layout's own index names.

    ``index.json`` is the second root a layout has -- the first being the snapshots it retains -- and it is
    the only place that records what a *packed* brain consists of: the manifest, and the layer per module.
    Two callers need that. Pruning has to keep what a tag names, and a reopened brain has to find the
    travelling index it cannot rebuild.

    Args:
        store (BlockStore): The store to read. One with no layout index yields nothing.

    Returns:
        list[PublishedArtifact]: What the index names, in the order it names them.
    """
    reader = getattr(store, "index", None)
    if reader is None:
        return []
    try:
        entries = reader().get("manifests", [])
    except Exception:
        # A layout whose index cannot be read has no tags as far as anything here is concerned.
        return []

    artifacts: list[PublishedArtifact] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("digest"), str):
            continue
        try:
            digest = OciDigest.parse(entry["digest"])
        except IdentityError:
            continue

        manifest: BrainManifest | None = None
        if store.is_resolvable(digest):
            try:
                manifest = parse_manifest(store.get_bytes(digest))
            except (DistributionError, BlockNotFoundError):
                manifest = None

        tag = (
            entry.get("annotations", {}).get(REF_NAME_ANNOTATION)
            if isinstance(entry.get("annotations"), dict)
            else None
        )
        artifacts.append(PublishedArtifact(digest=digest, tag=tag if isinstance(tag, str) else None, manifest=manifest))
    return artifacts


def parse_manifest(data: bytes) -> BrainManifest:
    """
    Parse a manifest pulled from a registry.

    Args:
        data (bytes): The manifest JSON.

    Returns:
        BrainManifest: The parsed manifest.

    Raises:
        DistributionError: If the artifact is not a Boltzmann brain, does not declare OCI's schema
            version, or declares a protocol version this client does not implement.
    """
    try:
        document: Any = json.loads(data)
    except json.JSONDecodeError as error:
        raise DistributionError(f"manifest is not valid JSON: {error}") from error

    if not isinstance(document, dict):
        raise DistributionError(f"manifest must be an object, got {type(document).__name__}")

    artifact_type = document.get("artifactType", document.get("artifact_type"))
    if artifact_type != ARTIFACT_TYPE:
        raise DistributionError(f"not a Boltzmann brain: artifactType is {artifact_type!r}, expected {ARTIFACT_TYPE!r}")

    version = document.get("schemaVersion", document.get("schema_version", OCI_SCHEMA_VERSION))
    if version != OCI_SCHEMA_VERSION:
        raise DistributionError(f"manifest declares schemaVersion {version!r}; OCI fixes it at {OCI_SCHEMA_VERSION}")

    # Every field here is registry-supplied, so its *type* is untrusted too. Calling ``.get`` on
    # whatever ``annotations`` happened to be turned a hostile manifest into an AttributeError rather
    # than the DistributionError this function documents.
    annotations = document.get("annotations", {})
    if not isinstance(annotations, dict):
        raise DistributionError(f"manifest annotations must be an object, got {type(annotations).__name__}")

    declared = annotations.get(ANNOTATION_PROTOCOL_VERSION)
    if declared is not None and declared != str(PROTOCOL_VERSION):
        raise DistributionError(
            f"artifact declares protocol version {declared!r}, this client implements {PROTOCOL_VERSION}"
        )

    return BrainManifest.model_validate(document)
