"""The registry interface and the shape of an install.

OCI contributes storage, registry authentication, transport, digest-based deduplication, and tags
(paper Section 7.3). None of that is the protocol's to implement, so this module declares what a
transport must offer and leaves the talking to an implementation.

Async, because this is where network I/O lives -- the kernel stays synchronous, since hashing and
Merkle construction have nothing to wait for.

Because the local brain is already an OCI layout, an implementation's push is a transfer of blobs
that already exist rather than a serialization step, and its pull writes blobs into a layout that is
immediately usable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId, OciDigest

if TYPE_CHECKING:
    from boltzmann.distribution.manifest import BrainManifest
    from boltzmann.store.base import BlockStore


class InstallPlan(BaseModel):
    """
    What an install or update would transfer, computed before anything is downloaded.

    To install only the episodic module, a client resolves the descriptor marked ``episodic`` and
    downloads that blob alone (paper Section 7.2). For an update, the new manifest reuses the
    digests of unchanged modules, so only what moved is fetched; inside the changed blob, the Merkle
    root reveals which logical blocks differ.

    Attributes:
        modules (list[MemoryType]): Which modules would be installed.
        fetch_layers (list[MemoryType]): Which module layers must be downloaded. Empty means the
            local brain already holds every needed layer.
        reuse_layers (list[MemoryType]): Which layers are reused by digest.
        fetch_blocks (list[BlockId]): Which individual blocks are missing locally, once the changed
            layer's root is compared against the installed one.
        rebuild_indices (list[str]): Which indices the client will regenerate rather than download,
            because they are deterministic functions of the blocks.
        fetch_vector_indices (list[MemoryType]): Which vector indices must be downloaded, being the
            one derived structure a model-agnostic client cannot rebuild.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    modules: list[MemoryType] = Field(default_factory=list)
    fetch_layers: list[MemoryType] = Field(default_factory=list)
    reuse_layers: list[MemoryType] = Field(default_factory=list)
    fetch_blocks: list[BlockId] = Field(default_factory=list)
    rebuild_indices: list[str] = Field(default_factory=list)
    fetch_vector_indices: list[MemoryType] = Field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        """Whether the local brain is already at the target state."""
        return not self.fetch_layers and not self.fetch_blocks and not self.fetch_vector_indices


@runtime_checkable
class RegistryClient(Protocol):
    """Talks to an OCI-compatible registry. Implemented by the caller."""

    async def resolve(self, reference: str, tag: str) -> BrainManifest:
        """
        Fetch a manifest by tag, without downloading any module.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to resolve.

        Returns:
            BrainManifest: The manifest.
        """
        ...

    async def pull_blob(self, reference: str, digest: OciDigest, store: BlockStore) -> None:
        """
        Download one blob into a local store.

        Args:
            reference (str): Repository reference.
            digest (OciDigest): The blob to fetch.
            store (BlockStore): Where to write it.
        """
        ...

    async def push(self, reference: str, tag: str, manifest: BrainManifest, store: BlockStore) -> OciDigest:
        """
        Publish an already-packed artifact.

        A transport receives a manifest rather than a snapshot because packing is not a network concern:
        the brain assembled the layers and the manifest locally, and every blob the manifest names is
        already in ``store``. All that is left is to move the ones the registry lacks.

        Args:
            reference (str): Repository reference.
            tag (str): The tag to publish under.
            manifest (BrainManifest): The manifest to publish.
            store (BlockStore): Where the blobs are read from.

        Returns:
            OciDigest: Digest of the pushed manifest.
        """
        ...
