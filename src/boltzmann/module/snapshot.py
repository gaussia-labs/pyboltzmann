"""A snapshot: which version of each module a brain currently is.

An installed brain is a set of module roots. The snapshot names them, and when the
brain is published it becomes the config blob of the OCI Artifact, so the same
document is both the local state and the wire format (paper Section 7).

A snapshot is a *logical* identity made of ``MerkleRoot`` values; its own digest is
an ``OciDigest``, because a snapshot document is a transportable file. Keeping those
two straight is the point of Section 6.4 of the paper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import SnapshotError
from boltzmann.identity.digest import MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize
from boltzmann.identity.time import Timestamp, utc_timestamp
from boltzmann.merkle.tree import LAYOUT_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable


class ModuleRef(BaseModel):
    """
    One module's version within a snapshot.

    Attributes:
        memory_type (MemoryType): Which module this is.
        root (MerkleRoot): The Merkle root that commits to its composition.
        composition (OciDigest): Content address of the composition document -- the leaf list the
            root commits to. A root can be verified but not inverted, so without this a snapshot
            would identify a version it could not reopen.
        block_count (int): How many blocks the composition holds.
        layout (str): Identifier of the Merkle layout that produced ``root``. Two
            implementations can only compare roots if they agree on this.
        embedding_model (str | None): Model and version that produced the module's
            vector index, when one travels with it. The vector index is the one
            derived structure a model-agnostic client cannot rebuild on its own
            (paper Section 6.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_type: MemoryType
    root: MerkleRoot
    composition: OciDigest
    block_count: int = Field(ge=0)
    layout: str = LAYOUT_NAME
    embedding_model: str | None = None


class Snapshot(BaseModel):
    """
    The state of a brain: one root per installed module.

    Attributes:
        boltzmann (int): Protocol version this snapshot conforms to.
        modules (dict[MemoryType, ModuleRef]): The installed modules. A brain may
            hold a subset: selective installation is the point of packaging each
            module separately (paper Section 7.2).
        created_at (Timestamp): When the snapshot was produced.
        parent (OciDigest | None): Digest of the snapshot this one succeeds, forming
            an auditable chain of versions.
        labels (dict[str, str] | None): Free-form annotations, such as a release tag.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    modules: dict[MemoryType, ModuleRef] = Field(default_factory=dict)
    created_at: Timestamp = Field(default_factory=utc_timestamp)
    parent: OciDigest | None = None
    labels: dict[str, str] | None = None

    # --- Access ---------------------------------------------------------------

    def root_of(self, memory_type: MemoryType) -> MerkleRoot:
        """
        The root of one installed module.

        Args:
            memory_type (MemoryType): Which module to look up.

        Returns:
            MerkleRoot: That module's root.

        Raises:
            SnapshotError: If the module is not installed.
        """
        reference = self.modules.get(memory_type)
        if reference is None:
            installed = ", ".join(sorted(kind.value for kind in self.modules)) or "none"
            raise SnapshotError(f"the {memory_type.value} module is not installed; installed: {installed}")
        return reference.root

    def has_module(self, memory_type: MemoryType) -> bool:
        """
        Whether a module is installed in this snapshot.

        Args:
            memory_type (MemoryType): Which module to check.

        Returns:
            bool: Whether it is present.
        """
        return memory_type in self.modules

    @property
    def installed(self) -> list[MemoryType]:
        """The installed modules, in the canonical module order."""
        return [kind for kind in MemoryType if kind in self.modules]

    @property
    def block_count(self) -> int:
        """Total number of blocks across every installed module."""
        return sum(reference.block_count for reference in self.modules.values())

    # --- Identity -------------------------------------------------------------

    def canonical_bytes(self) -> bytes:
        """
        The snapshot as canonical bytes, which is what gets published as the config blob.

        Returns:
            bytes: The canonically serialized snapshot.
        """
        return canonicalize(self.model_dump(mode="json", exclude_none=True))

    @property
    def digest(self) -> OciDigest:
        """The snapshot document's physical identity."""
        return OciDigest.of(self.canonical_bytes())

    # --- Derivation -----------------------------------------------------------

    def with_modules(self, references: Iterable[ModuleRef]) -> Snapshot:
        """
        Derive one successor snapshot in which several modules have advanced.

        A commit can touch more than one module -- adding a semantic block also advances provenance --
        and that is still **one** version of the brain. Advancing them one at a time would mint
        intermediate snapshots that were never published, and the ``parent`` chain would then point at
        documents no consumer can resolve.

        An incremental update touches only the changed modules: every other module keeps the root it
        already had, which is what lets a consumer download one blob instead of the whole brain
        (paper Section 7.3).

        Args:
            references (Iterable[ModuleRef]): The modules' new versions.

        Returns:
            Snapshot: The successor snapshot, chained to this one.
        """
        advanced = {**self.modules}
        for reference in references:
            advanced[reference.memory_type] = reference
        return Snapshot(
            boltzmann=self.boltzmann,
            modules=advanced,
            created_at=utc_timestamp(),
            parent=self.digest,
            labels=self.labels,
        )

    def with_module(self, reference: ModuleRef) -> Snapshot:
        """
        Derive a snapshot in which one module has advanced to a new root.

        Args:
            reference (ModuleRef): The module's new version.

        Returns:
            Snapshot: The successor snapshot, chained to this one.
        """
        return self.with_modules([reference])

    def without_module(self, memory_type: MemoryType) -> Snapshot:
        """
        Derive a snapshot with one module uninstalled.

        Args:
            memory_type (MemoryType): The module to remove.

        Returns:
            Snapshot: The successor snapshot, chained to this one.
        """
        remaining = {kind: reference for kind, reference in self.modules.items() if kind is not memory_type}
        return Snapshot(
            boltzmann=self.boltzmann,
            modules=remaining,
            created_at=utc_timestamp(),
            parent=self.digest,
            labels=self.labels,
        )

    @classmethod
    def of(cls, references: Iterable[ModuleRef], labels: dict[str, str] | None = None) -> Snapshot:
        """
        Build a snapshot from a set of module versions.

        Args:
            references (Iterable[ModuleRef]): The installed modules.
            labels (dict[str, str] | None): Optional annotations.

        Returns:
            Snapshot: The snapshot naming those versions.
        """
        return cls(
            modules={reference.memory_type: reference for reference in references},
            labels=labels,
        )
