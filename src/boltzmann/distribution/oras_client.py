"""The network transport, over ORAS.

Requires the ``[oci]`` extra. The paper cites ORAS as the reference for OCI Artifacts, and this is a
thin adapter over it: resolve a manifest, fetch a blob, upload the blobs a registry does not have, then
the manifest.

Because the local brain is already an OCI layout, a push here is a transfer of files that already exist
on disk -- ORAS uploads a path, and that path is the blob in ``blobs/sha256/``. Nothing is serialized at
push time.

Blob-level methods are used rather than ORAS's file-oriented ``push``/``pull``, because the artifact
this SDK publishes has its own manifest shape: one layer per module, each annotated with the module's
internal Merkle root, and a config blob that is the snapshot document.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from boltzmann.distribution.manifest import BrainManifest, parse_manifest
from boltzmann.distribution.media_types import (
    ARTIFACT_TYPE,
    MANIFEST_MEDIA_TYPE,
    REF_NAME_ANNOTATION,
)
from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import OciDigest
from boltzmann.store.oci_layout import OciLayoutStore

if TYPE_CHECKING:
    from boltzmann.distribution.manifest import Descriptor
    from boltzmann.store.base import BlockStore


def _registry(insecure: bool) -> Any:
    """Import ORAS lazily, so the core stays installable without the extra."""
    try:
        import oras.provider
    except ModuleNotFoundError as error:  # pragma: no cover - depends on install extras
        raise DistributionError("the ORAS transport needs the [oci] extra: pip install 'boltzmann[oci]'") from error
    return oras.provider.Registry(insecure=insecure)


class OrasRegistryClient:
    """
    Talks to an OCI-compatible registry over HTTP.

    Attributes:
        insecure (bool): Whether to allow plain HTTP, for local registries.
    """

    def __init__(self, insecure: bool = False, registry: Any | None = None) -> None:
        """
        Build a client.

        Args:
            insecure (bool): Whether to allow plain HTTP.
            registry (Any | None): An already-configured ORAS ``Registry``, for callers that handled
                authentication themselves. Defaults to a fresh one.
        """
        self.insecure = insecure
        self._registry = registry

    @property
    def registry(self) -> Any:
        """The underlying ORAS registry, built on first use."""
        if self._registry is None:
            self._registry = _registry(self.insecure)
        return self._registry

    def login(self, username: str, password: str) -> None:
        """
        Authenticate against the registry.

        Args:
            username (str): Account name.
            password (str): Password or token.
        """
        self.registry.login(username=username, password=password)

    async def resolve(self, reference: str, tag: str) -> BrainManifest:
        """
        Fetch a manifest by tag, without downloading any module.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to resolve.

        Returns:
            BrainManifest: The manifest.

        Raises:
            DistributionError: If the tag cannot be resolved or is not a Boltzmann brain.
        """
        import json

        try:
            document = self.registry.get_manifest(f"{reference}:{tag}")
        except Exception as error:
            raise DistributionError(f"cannot resolve {reference}:{tag}: {error}") from error
        return parse_manifest(json.dumps(self._from_oci(document)).encode())

    async def pull_blob(self, reference: str, digest: OciDigest, store: BlockStore) -> None:
        """
        Download one blob into a local store.

        Args:
            reference (str): Repository reference.
            digest (OciDigest): The blob to fetch.
            store (BlockStore): Where to write it. The store recomputes the digest, so a corrupted
                download cannot land under the identity it claims.

        Raises:
            DistributionError: If the download fails.
        """
        try:
            response = self.registry.get_blob(reference, str(digest))
            response.raise_for_status()
        except Exception as error:
            raise DistributionError(f"cannot fetch {digest.short} from {reference}: {error}") from error

        stored = store.put_bytes(response.content)
        if stored != digest:
            raise DistributionError(f"{reference} served bytes for {digest.short} that hash to {stored.short}")

    async def push(
        self,
        reference: str,
        tag: str,
        manifest: BrainManifest,
        store: BlockStore,
    ) -> OciDigest:
        """
        Upload the blobs the registry lacks, then the manifest.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to publish under.
            manifest (BrainManifest): The manifest to publish.
            store (BlockStore): Where the blobs are read from.

        Returns:
            OciDigest: Digest of the pushed manifest.

        Raises:
            DistributionError: If an upload fails.
        """
        container = self.registry.get_container(f"{reference}:{tag}")

        for descriptor in [manifest.config, *manifest.layers]:
            layer = self._to_oci_descriptor(descriptor)
            if self.registry.blob_exists(layer, container):
                continue
            with self._as_file(descriptor, store) as path:
                response = self.registry.upload_blob(str(path), container, layer)
            if response.status_code not in (200, 201, 202):
                raise DistributionError(
                    f"uploading {descriptor.digest.short} to {reference} failed with {response.status_code}"
                )

        document = self._to_oci(manifest, tag)
        response = self.registry.upload_manifest(document, container)
        if response.status_code not in (200, 201, 202):
            raise DistributionError(f"publishing {reference}:{tag} failed with {response.status_code}")
        return OciDigest.of(manifest.to_bytes())

    # --- Wire shape -----------------------------------------------------------

    def _to_oci_descriptor(self, descriptor: Descriptor) -> dict[str, Any]:
        """A Boltzmann descriptor in OCI's camelCase wire shape."""
        payload: dict[str, Any] = {
            "mediaType": descriptor.media_type,
            "digest": str(descriptor.digest),
            "size": descriptor.size,
        }
        if descriptor.annotations:
            payload["annotations"] = dict(descriptor.annotations)
        return payload

    def _to_oci(self, manifest: BrainManifest, tag: str) -> dict[str, Any]:
        """The manifest in OCI's wire shape, which is camelCase and nests differently."""
        return {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": manifest.artifact_type,
            "config": self._to_oci_descriptor(manifest.config),
            "layers": [self._to_oci_descriptor(layer) for layer in manifest.layers],
            "annotations": {**manifest.annotations, REF_NAME_ANNOTATION: tag},
        }

    def _from_oci(self, document: dict[str, Any]) -> dict[str, Any]:
        """OCI's wire shape back into this SDK's manifest fields."""
        if document.get("artifactType") != ARTIFACT_TYPE:
            raise DistributionError(f"not a Boltzmann brain: artifactType is {document.get('artifactType')!r}")

        def descriptor(payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "media_type": payload["mediaType"],
                "digest": payload["digest"],
                "size": payload["size"],
                "annotations": payload.get("annotations", {}),
            }

        annotations = {
            key: value for key, value in document.get("annotations", {}).items() if key != REF_NAME_ANNOTATION
        }
        return {
            "artifact_type": document["artifactType"],
            "config": descriptor(document["config"]),
            "layers": [descriptor(layer) for layer in document.get("layers", [])],
            "annotations": annotations,
        }

    def _as_file(self, descriptor: Descriptor, store: BlockStore) -> Any:
        """ORAS uploads a path. For a layout store that path already exists; otherwise, stage one."""
        if isinstance(store, OciLayoutStore):
            return _ExistingFile(store.blobs_dir / descriptor.digest.hex)
        return _StagedFile(store.get_bytes(descriptor.digest))


class _ExistingFile:
    """A blob that is already a file on disk, so there is nothing to copy."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *exception: object) -> None:
        return None


class _StagedFile:
    """A blob from a store with no filesystem, staged for the duration of one upload."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self._handle: Any = None

    def __enter__(self) -> Path:
        self._handle = tempfile.NamedTemporaryFile(delete=False)
        self._handle.write(self.payload)
        self._handle.close()
        return Path(self._handle.name)

    def __exit__(self, *exception: object) -> None:
        if self._handle is not None:
            Path(self._handle.name).unlink(missing_ok=True)
