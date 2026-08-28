"""A snapshot: which version of each module a brain currently is.

An installed brain is a set of module roots. The snapshot names them, and when the
brain is published it becomes the config blob of the OCI Artifact, so the same
document is both the local state and the wire format (paper Section 7).

A snapshot is a *logical* identity made of ``MerkleRoot`` values; its own digest is
an ``OciDigest``, because a snapshot document is a transportable file. Keeping those
two straight is the point of Section 6.4 of the paper.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boltzmann.authenticity.trust_root import TrustRoot
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import SerializationError, SnapshotError
from boltzmann.identity.digest import MerkleRoot, OciDigest
from boltzmann.identity.serialization import canonicalize, parse_json_strict
from boltzmann.identity.time import Timestamp, utc_timestamp
from boltzmann.merkle.tree import LAYOUT_NAME


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
        index_digest (OciDigest | None): Digest of the travelling index payload. This places the
            index inside the signed snapshot bytes instead of trusting the registry manifest.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_type: MemoryType
    root: MerkleRoot
    composition: OciDigest
    block_count: int = Field(ge=0)
    layout: str = LAYOUT_NAME
    embedding_model: str | None = None
    index_digest: OciDigest | None = None

    @model_validator(mode="after")
    def _require_a_model_for_a_bound_index(self) -> Self:
        """A digest without the model that produced its vectors is not usable safely.

        The reverse is accepted for compatibility with v0.7.0 snapshots. Those legacy references
        remain readable, but their unbound index layers are never trusted or loaded.
        """
        if self.index_digest is not None and self.embedding_model is None:
            raise ValueError("a module reference with index_digest must also name its embedding_model")
        return self


class Snapshot(BaseModel):
    """
    The state of a brain: one root per installed module.

    Attributes:
        boltzmann (int): Protocol version this snapshot conforms to.
        modules (dict[MemoryType, ModuleRef]): The installed modules. A brain may
            hold a subset: selective installation is the point of packaging each
            module separately (paper Section 7.2).
        created_at (Timestamp): When the snapshot was produced.
        parents (list[OciDigest]): The snapshots this one succeeds, forming an auditable
            history. A linear history is the ordinary case and carries one entry; a root
            snapshot carries none; a reconciliation carries two or more
            (paper Section 12.1).
        labels (dict[str, str] | None): Free-form annotations, such as a release tag.
        trust_root (TrustRoot | None): The keys authorized to sign for this brain, and the scopes
            each one holds (paper Section 8.5). Carried here rather than in a module or a layer
            because it MUST reach every install, complete or partial, and because living inside
            the signed document means a signature can never be evaluated against a key list the
            signer did not commit to. ``None`` is the zero-configuration case: a brain with no
            authorship from the outset, fully verifiable for integrity. A ``None`` never enters
            the canonical bytes, so a brain that never adopts a trust root keeps the exact digests
            it had before this field existed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    modules: dict[MemoryType, ModuleRef] = Field(default_factory=dict)
    created_at: Timestamp = Field(default_factory=utc_timestamp)
    parents: list[OciDigest] = Field(default_factory=list)
    labels: dict[str, str] | None = None
    trust_root: TrustRoot | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_scalar_parent(cls, data: Any) -> Any:
        """
        Read a document written before ``parents`` was a list.

        Every snapshot published under the scalar form is still a valid statement of composition, and
        it is immutable: refusing it would make the histories that already exist unreadable in order
        to gain nothing. So the scalar is accepted on the way in and normalized to a one-element list.

        A document carrying *both* keys is refused rather than reconciled. It says two things about the
        same lineage, and there is no reading of it that is not a guess -- the same reason this model
        sets ``extra="forbid"``.
        """
        if not isinstance(data, dict) or "parent" not in data:
            return data
        if "parents" in data:
            raise ValueError(
                "a snapshot document names both 'parent' and 'parents'; the two state the same lineage "
                "and a document that carries both cannot be read without guessing which one is meant"
            )
        scalar = data["parent"]
        rest = {key: value for key, value in data.items() if key != "parent"}
        return {**rest, "parents": [] if scalar is None else [scalar]}

    @model_validator(mode="after")
    def _reject_repeated_parents(self) -> Self:
        """A history named twice is one history. Listing it twice would make ``parents`` order-dependent
        for a question that is set-shaped, and no reconciliation needs it."""
        if len(set(self.parents)) != len(self.parents):
            named = ", ".join(digest.short for digest in self.parents)
            raise ValueError(f"a snapshot names the same parent more than once: {named}")
        return self

    # --- Lineage --------------------------------------------------------------

    @property
    def first_parent(self) -> OciDigest | None:
        """
        The history this snapshot was produced onto.

        Order is significant in exactly one way (paper Section 12.1): the first parent is the history a
        reconciliation was performed onto, and every rule that speaks of "the parent" means this one --
        the scope a signature must hold, the trust root in force, and the difference a consumer reads
        as the change this snapshot made. The remaining parents are merged-in history, and **no
        authorization is derived from them**.

        Returns:
            OciDigest | None: The first parent, or ``None`` for a root snapshot.
        """
        return self.parents[0] if self.parents else None

    @property
    def is_reconciliation(self) -> bool:
        """Whether this snapshot names more than one parent, and therefore joined two histories."""
        return len(self.parents) > 1

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

        One parent is written as the scalar ``parent``, two or more as the list ``parents``, and a root
        snapshot writes neither. This is the rule of Section 6.6 applied to the snapshot document
        rather than to a block: a version is a statement, not a preference, so a snapshot is written
        under the oldest form that can express it. A linear history therefore keeps the exact bytes --
        and the exact digest -- it had before ``parents`` existed, and a client that has no notion of
        reconciliation stops being able to read a brain only at the point where that brain genuinely
        reconciled something, which is the one document it could not have interpreted anyway.

        Returns:
            bytes: The canonically serialized snapshot.
        """
        document = self.model_dump(mode="json", exclude_none=True)
        parents = document.pop("parents", [])
        if len(parents) == 1:
            document["parent"] = parents[0]
        elif parents:
            document["parents"] = parents
        return canonicalize(document)

    @classmethod
    def from_document(cls, data: bytes) -> Snapshot:
        """Decode one canonical snapshot document without ambiguous JSON semantics.

        Args:
            data (bytes): The config or history bytes that physically identify the snapshot.

        Returns:
            Snapshot: The validated snapshot.

        Raises:
            SnapshotError: If the bytes are ambiguous JSON or are not the canonical representation
                of the snapshot they decode to.
            pydantic.ValidationError: If the decoded object does not satisfy the snapshot schema.
        """
        try:
            document = parse_json_strict(data)
        except SerializationError as error:
            raise SnapshotError(f"snapshot document {error}") from error
        snapshot = cls.model_validate(document)
        if snapshot.canonical_bytes() != data:
            raise SnapshotError("snapshot document is not in canonical jcs/1 form")
        return snapshot

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
        # The trust root is carried forward verbatim on every derivation. A commit is not a
        # governance act: if a derivation dropped it, an ordinary commit would present a changed
        # trust-root digest (present -> absent) to the verifier, be classified as a revision, and
        # demand a ``govern`` quorum it has no reason to carry. Propagation is what keeps "the
        # trust root changed" a statement about governance rather than about which constructor ran.
        return Snapshot(
            boltzmann=self.boltzmann,
            modules=advanced,
            created_at=utc_timestamp(),
            parents=[self.digest],
            labels=self.labels,
            trust_root=self.trust_root,
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

    def reconciled(self, references: Iterable[ModuleRef], merged: Iterable[OciDigest]) -> Snapshot:
        """
        Derive a snapshot that joins this history with one or more others.

        This is the whole structural change reconciliation needed (paper Section 12.1): no new document,
        a field that holds more than one entry. ``self`` becomes the **first** parent, which is what
        records that the reconciliation was performed onto this history, and the histories in ``merged``
        become merged-in parents that grant nothing.

        Args:
            references (Iterable[ModuleRef]): The reconciled modules' versions. Unlike
                :meth:`with_modules`, this replaces the module set outright rather than advancing part
                of it: a reconciliation states the composition of every module it names, including the
                ones whose root it took from the other side unchanged.
            merged (Iterable[OciDigest]): The other histories being joined, in the order they should be
                recorded.

        Returns:
            Snapshot: The reconciliation.

        Raises:
            SnapshotError: If no other history was given, or if one of them is this snapshot. A
                reconciliation with nothing is a commit, and a history merged with itself is not a
                second history -- both are almost certainly a caller bug, and both would otherwise
                produce a snapshot that misrepresents what happened.
        """
        others = list(merged)
        if not others:
            raise SnapshotError(
                "a reconciliation names at least one other history; to advance this one, use with_modules"
            )
        if self.digest in others:
            raise SnapshotError(
                f"snapshot {self.digest.short} cannot be merged with itself: it is already the first parent"
            )
        # The trust root is the FIRST parent's, never the other side's: a merge does not adopt a
        # key list. Reconciling two histories that carry different trust roots is refused upstream
        # as a governance conflict -- unioning two key lists would grant the union of both sides'
        # permissions, which defeats the quorum rule outright (paper Section 12.5).
        return Snapshot(
            boltzmann=self.boltzmann,
            modules={reference.memory_type: reference for reference in references},
            created_at=utc_timestamp(),
            parents=[self.digest, *others],
            labels=self.labels,
            trust_root=self.trust_root,
        )

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
            parents=[self.digest],
            labels=self.labels,
            trust_root=self.trust_root,
        )

    def with_trust_root(self, trust_root: TrustRoot) -> Snapshot:
        """
        Derive a trust-root revision: the key list changes and nothing else does.

        The modules are copied verbatim from this snapshot, so the constructor is structurally
        incapable of folding a content change into a governance act -- the paper requires a
        revision's module roots to equal its first parent's (Section 8.5), and here that is not a
        rule to check but a thing that cannot be otherwise. The verifier still re-checks it,
        because it also meets revisions this SDK did not build.

        The revision this snapshot introduces is not valid on its own: it must be covered by at
        least ``govern_quorum`` signatures from distinct keys holding ``govern`` in the trust
        root as it stood *before* the change. Collecting those signatures and refusing to advance
        without them is the caller's job (``Brain.rotate``); this method only shapes the document.

        Args:
            trust_root (TrustRoot): The new key list.

        Returns:
            Snapshot: The revision, chained to this snapshot.

        Raises:
            SnapshotError: If this snapshot already carries a trust root whose revision is not
                strictly below the new one. Equal covers the byte-identical case -- a revision
                that revises nothing would demand a quorum for no change -- and lower would make
                "the revision in force" ambiguous between two documents.
        """
        if self.trust_root is not None and trust_root.revision <= self.trust_root.revision:
            raise SnapshotError(
                f"a trust-root revision must exceed the one in force: revision {trust_root.revision} "
                f"does not follow {self.trust_root.revision}"
            )
        return Snapshot(
            boltzmann=self.boltzmann,
            modules=self.modules,
            created_at=utc_timestamp(),
            parents=[self.digest],
            labels=self.labels,
            trust_root=trust_root,
        )

    @classmethod
    def of(
        cls,
        references: Iterable[ModuleRef],
        labels: dict[str, str] | None = None,
        trust_root: TrustRoot | None = None,
    ) -> Snapshot:
        """
        Build a snapshot from a set of module versions.

        Args:
            references (Iterable[ModuleRef]): The installed modules.
            labels (dict[str, str] | None): Optional annotations.
            trust_root (TrustRoot | None): The keys authorized to sign for this brain, if it has any.

        Returns:
            Snapshot: The snapshot naming those versions.
        """
        return cls(
            modules={reference.memory_type: reference for reference in references},
            labels=labels,
            trust_root=trust_root,
        )
