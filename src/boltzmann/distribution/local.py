"""A registry backed by an OCI layout on disk.

Not a stand-in for a real registry: OCI layouts are a first-class transport target, which is why
``oras copy --to-oci-layout`` exists. This is how a brain moves between two machines that share a
filesystem or a USB stick and never touch a network, and it is how the distribution path gets tested
without one.

It satisfies the same :class:`~boltzmann.distribution.registry.RegistryClient` interface as the network
transport, so nothing above it can tell the difference.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from boltzmann.distribution.manifest import BrainManifest, parse_manifest
from boltzmann.distribution.media_types import ARTIFACT_TYPE, MANIFEST_MEDIA_TYPE, REF_NAME_ANNOTATION
from boltzmann.exceptions import DistributionError, ReferenceNotFoundError
from boltzmann.identity.digest import OciDigest
from boltzmann.store.oci_layout import OciLayoutStore

if TYPE_CHECKING:
    from boltzmann.store.base import BlockStore


class LocalLayoutRegistry:
    """
    Publishes to and reads from OCI layouts under a base directory.

    Attributes:
        root (Path): Base directory. Each repository reference becomes a layout beneath it.
    """

    def __init__(self, root: Path | str) -> None:
        """
        Point a client at a directory of layouts.

        Args:
            root (Path | str): Base directory.
        """
        self.root = Path(root)

    def layout(self, reference: str, create: bool = False) -> OciLayoutStore:
        """
        The layout backing one repository reference.

        A reference names a repository *within* this registry, so it has to resolve inside the
        root. Joining it straight on did not guarantee that: ``pathlib`` lets an absolute
        reference replace the root outright, and ``..`` walks out of it, so a reference arriving
        from configuration or a manifest could address anything the process can reach. It is
        checked rather than sanitised -- quietly rewriting a reference would file an artifact
        under a name the caller did not ask for.

        Args:
            reference (str): Repository reference. Path separators in it become directories.
            create (bool): Whether to initialize the layout if absent.

        Returns:
            OciLayoutStore: The layout.

        Raises:
            DistributionError: If the reference escapes the registry root, or the layout does not
                exist and ``create`` is false.
        """
        path = self.root / reference.replace(":", "_")
        root = self.root.resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise DistributionError(
                f"reference {reference!r} resolves to {resolved}, which is outside the registry root {root}"
            )
        if not create and not (path / "oci-layout").is_file():
            raise DistributionError(f"no artifact published at {reference}")
        return OciLayoutStore(path, create=create)

    async def resolve(self, reference: str, tag: str) -> BrainManifest:
        """
        Read a manifest by tag.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to resolve.

        Returns:
            BrainManifest: The manifest.

        Raises:
            ReferenceNotFoundError: If the repository or the tag is not published. The same distinction the
                network transport draws, so that a caller behaves identically against either.
        """
        try:
            store = self.layout(reference)
        except DistributionError as error:
            raise ReferenceNotFoundError(f"{reference}:{tag} is not published") from error
        for entry in store.index().get("manifests", []):
            if entry.get("annotations", {}).get(REF_NAME_ANNOTATION) == tag:
                return parse_manifest(store.get_bytes(OciDigest.parse(entry["digest"])))
        published = [
            entry.get("annotations", {}).get(REF_NAME_ANNOTATION) for entry in store.index().get("manifests", [])
        ]
        tags = ", ".join(name for name in published if name) or "none"
        raise ReferenceNotFoundError(f"{reference} has no tag {tag!r}; published tags: {tags}")

    async def pull_blob(self, reference: str, digest: OciDigest, store: BlockStore) -> None:
        """
        Copy one blob into a local store.

        Args:
            reference (str): Repository reference.
            digest (OciDigest): The blob to fetch.
            store (BlockStore): Where to write it.
        """
        store.put_bytes(self.layout(reference).get_bytes(digest))

    async def push(
        self,
        reference: str,
        tag: str,
        manifest: BrainManifest,
        store: BlockStore,
    ) -> OciDigest:
        """
        Copy an artifact's blobs and manifest into the target layout.

        Only blobs the target does not already hold are copied, which is what makes an update that
        changed one module transfer one layer.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to publish under.
            manifest (BrainManifest): The manifest to publish.
            store (BlockStore): Where the blobs are read from.

        Returns:
            OciDigest: Digest of the published manifest.
        """
        target = self.layout(reference, create=True)

        for descriptor in [manifest.config, *manifest.layers]:
            if not target.is_resolvable(descriptor.digest):
                target.put_bytes(store.get_bytes(descriptor.digest))

        payload = manifest.to_bytes()
        digest = target.put_bytes(payload)

        index = target.index()
        entry: dict[str, Any] = {
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": ARTIFACT_TYPE,
            "digest": str(digest),
            "size": len(payload),
            "annotations": {REF_NAME_ANNOTATION: tag},
        }
        kept = [
            existing
            for existing in index.get("manifests", [])
            if existing.get("annotations", {}).get(REF_NAME_ANNOTATION) != tag
        ]
        index["manifests"] = [*kept, entry]
        target.write_index(index)
        return digest

    def tags(self, reference: str) -> list[str]:
        """
        Which tags a repository publishes.

        Args:
            reference (str): Repository reference.

        Returns:
            list[str]: The published tags.
        """
        return [
            name
            for entry in self.layout(reference).index().get("manifests", [])
            if (name := entry.get("annotations", {}).get(REF_NAME_ANNOTATION))
        ]
