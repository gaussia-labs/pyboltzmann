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

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.distribution.manifest import BrainManifest, Descriptor, SignatureManifest
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.snapshot import Snapshot
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
        ignored_vector_indices (list[MemoryType]): Which published vector indices the caller chose not
            to download. Their modules are still installed; the caller is responsible for supplying or
            rebuilding a compatible index if semantic retrieval is required.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    modules: list[MemoryType] = Field(default_factory=list)
    fetch_layers: list[MemoryType] = Field(default_factory=list)
    reuse_layers: list[MemoryType] = Field(default_factory=list)
    fetch_blocks: list[BlockId] = Field(default_factory=list)
    rebuild_indices: list[str] = Field(default_factory=list)
    fetch_vector_indices: list[MemoryType] = Field(default_factory=list)
    ignored_vector_indices: list[MemoryType] = Field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        """Whether the local brain is already at the target state."""
        return not self.fetch_layers and not self.fetch_blocks and not self.fetch_vector_indices


class FetchResult(BaseModel):
    """
    A remote history retrieved without moving the local pointer.

    ``fetch`` exists because incorporating a contribution has a step where *nothing has changed yet*
    (paper Section 12.6): the maintainer holds two histories locally while the published brain is
    untouched, and only then judges the incoming blocks. Folding that into ``pull`` would mean adopting
    a history in order to inspect it.

    Attributes:
        reference (str): Repository the history came from.
        tag (str): The tag that was resolved.
        snapshot (Snapshot): The remote head, as published. Its parents are what a common-ancestor
            search walks.
        digest (OciDigest): Content address of that snapshot document, which is the identity a
            reconciliation records as a merged parent.
        modules (list[MemoryType]): Which modules were retrieved.
        incoming (dict[MemoryType, list[BlockId]]): Per module, the blocks the retrieved history holds
            that the installed one does not. This is the delta and nothing more: because everything is
            content-addressed, a contribution shares every block it did not change with the local brain
            byte for byte, so a contribution of forty blocks is forty blocks, not a brain. These are
            the blocks a reconciliation puts through the validation gate as candidates.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reference: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    snapshot: Snapshot
    digest: OciDigest
    modules: list[MemoryType] = Field(default_factory=list)
    incoming: dict[MemoryType, list[BlockId]] = Field(default_factory=dict)

    @property
    def block_count(self) -> int:
        """How many blocks this history holds that the installed one does not, across every module."""
        return sum(len(blocks) for blocks in self.incoming.values())


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


@runtime_checkable
class RegistryReferrers(Protocol):
    """
    The optional referrers surface: signatures published *around* an artifact.

    A separate protocol rather than three more methods on :class:`RegistryClient`, because adding
    methods to a protocol breaks every third-party client that already satisfies it. A transport
    that implements only :class:`RegistryClient` stays conforming; it simply cannot carry
    signatures over the wire, and callers feature-detect this the way ``published_artifacts``
    already feature-detects a store's index.
    """

    async def push_referrer(self, reference: str, manifest: SignatureManifest, store: BlockStore) -> OciDigest:
        """
        Publish a signature manifest referring to an artifact already pushed.

        The referred artifact is never touched: that is the entire point. On a registry without
        the Referrers API the transport maintains the ``sha256-<hex>`` fallback tag instead.

        Args:
            reference (str): Repository reference.
            manifest (SignatureManifest): What to publish.
            store (BlockStore): Where the record blob and the empty config are read from.

        Returns:
            OciDigest: Digest of the published signature manifest.
        """
        ...

    async def referrers(self, reference: str, subject: OciDigest, artifact_type: str | None = None) -> list[Descriptor]:
        """
        Discover the manifests referring to one subject.

        Args:
            reference (str): Repository reference.
            subject (OciDigest): The referred manifest's digest.
            artifact_type (str | None): Narrow to one artifact type, when the registry can.

        Returns:
            list[Descriptor]: One descriptor per referrer, possibly empty.
        """
        ...

    async def pull_referrer(self, reference: str, digest: OciDigest) -> SignatureManifest:
        """
        Fetch one signature manifest by digest.

        A separate method from ``pull_blob`` because registries serve manifests through the
        manifests API, not the blobs API.

        Args:
            reference (str): Repository reference.
            digest (OciDigest): The signature manifest's digest.

        Returns:
            SignatureManifest: The parsed manifest.
        """
        ...
