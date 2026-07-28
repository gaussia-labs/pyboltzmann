"""The on-disk brain, which *is* an OCI Image Layout.

The paper distributes a brain as an OCI Artifact with one blob per module
(Section 7). This SDK closes the storage question by making the local brain an OCI
Image Layout directly, rather than a private format that gets converted at publish
time:

.. code-block:: text

    <root>/
      oci-layout              {"imageLayoutVersion": "1.0.0"}
      index.json              the image index; manifests land here when published
      blobs/sha256/<hex>      every block envelope and every observed blob
      boltzmann/              sidecar state that is not content (tombstones, indices)

Two consequences follow. Publishing is a copy, not a conversion, so selective
installation and incremental update fall out of the layout rather than being
re-implemented over it. And digest-based deduplication is the filesystem's job: two
identical originals are one file, which is what makes re-registering a source a
genuine no-op.

Derived indices live under ``boltzmann/`` and deliberately outside ``blobs/``: they
are views that can be rebuilt, not content that a root commits to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from boltzmann.exceptions import BlockNotFoundError, BlockTombstonedError, ModuleError
from boltzmann.identity.digest import Digest, OciDigest
from boltzmann.identity.hashing import ALGORITHM
from boltzmann.store.base import AbstractBlockStore

if TYPE_CHECKING:
    from collections.abc import Iterator

IMAGE_LAYOUT_VERSION = "1.0.0"
"""Version of the OCI Image Layout specification this store writes."""

IMAGE_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
"""Media type of ``index.json``."""

LAYOUT_MARKER = "oci-layout"
INDEX_FILE = "index.json"
BLOBS_DIR = "blobs"
SIDECAR_DIR = "boltzmann"
TOMBSTONES_FILE = "tombstones.json"


class OciLayoutStore(AbstractBlockStore):
    """
    A content-addressed store backed by an OCI Image Layout on disk.

    Attributes:
        root (Path): Directory holding the layout.
    """

    def __init__(self, root: Path | str, create: bool = True) -> None:
        """
        Open or create a brain layout.

        Args:
            root (Path | str): Directory of the layout.
            create (bool): Whether to initialize the layout if absent.

        Raises:
            ModuleError: If the directory exists but is not a usable OCI layout.
        """
        self.root = Path(root)
        if create:
            self._initialize()
        else:
            self._require_layout()
        self._tombstone_cache: dict[str, str] | None = None

    # --- Layout ---------------------------------------------------------------

    @property
    def blobs_dir(self) -> Path:
        """Directory holding content-addressed bytes, one file per digest."""
        return self.root / BLOBS_DIR / ALGORITHM

    @property
    def sidecar_dir(self) -> Path:
        """Directory holding state that is not content: tombstones, indices."""
        return self.root / SIDECAR_DIR

    def _initialize(self) -> None:
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.sidecar_dir.mkdir(parents=True, exist_ok=True)

        marker = self.root / LAYOUT_MARKER
        if not marker.exists():
            self._write_json(marker, {"imageLayoutVersion": IMAGE_LAYOUT_VERSION})

        index = self.root / INDEX_FILE
        if not index.exists():
            self._write_json(
                index,
                {"schemaVersion": 2, "mediaType": IMAGE_INDEX_MEDIA_TYPE, "manifests": []},
            )

    def _require_layout(self) -> None:
        marker = self.root / LAYOUT_MARKER
        if not marker.is_file():
            raise ModuleError(f"{self.root} is not an OCI layout: {LAYOUT_MARKER} is missing")
        version = json.loads(marker.read_text()).get("imageLayoutVersion")
        if version != IMAGE_LAYOUT_VERSION:
            raise ModuleError(
                f"{self.root} declares image layout version {version!r}, expected {IMAGE_LAYOUT_VERSION!r}"
            )
        if not self.blobs_dir.is_dir():
            raise ModuleError(f"{self.root} is not an OCI layout: {BLOBS_DIR}/{ALGORITHM} is missing")

    def index(self) -> dict[str, Any]:
        """
        Read the image index.

        Returns:
            dict[str, Any]: The parsed ``index.json``. Manifests are appended by the
            distribution layer when a snapshot is published.
        """
        index: dict[str, Any] = json.loads((self.root / INDEX_FILE).read_text())
        return index

    def write_index(self, index: dict[str, Any]) -> None:
        """
        Replace the image index.

        Writing a manifest into the index is what turns this directory from an OCI *layout* into a
        directory that carries an *artifact*, so any OCI tool can copy it without going through this
        SDK.

        Args:
            index (dict[str, Any]): The index document to write.
        """
        self._write_json(self.root / INDEX_FILE, index)

    # --- Physical layer -------------------------------------------------------

    def _path_for(self, digest: Digest) -> Path:
        return self.blobs_dir / digest.hex

    def put_bytes(self, data: bytes) -> OciDigest:
        """
        Store bytes and return their content address.

        The write goes to a temporary file and is renamed into place, so a reader
        never observes a partially written blob.

        Args:
            data (bytes): The bytes to store.

        Returns:
            OciDigest: The content address of the stored bytes.
        """
        digest = OciDigest.of(data)
        target = self._path_for(digest)
        if target.exists():
            return digest
        scratch = target.with_name(f".tmp-{digest.hex}")
        scratch.write_bytes(data)
        scratch.replace(target)
        return digest

    def _load(self, digest: Digest) -> bytes:
        reason = self._tombstones().get(digest.hex)
        if reason is not None:
            raise BlockTombstonedError(f"{digest.KIND} {digest.short} was redacted: {reason}")
        try:
            return self._path_for(digest).read_bytes()
        except FileNotFoundError:
            raise BlockNotFoundError(f"{digest.KIND} {digest.short} is not in {self.root}") from None

    def has(self, digest: Digest) -> bool:
        """
        Whether the digest is known, resolvable or tombstoned.

        Args:
            digest (Digest): The content address to look for.

        Returns:
            bool: Whether the digest is known.
        """
        return self._path_for(digest).exists() or digest.hex in self._tombstones()

    def is_resolvable(self, digest: Digest) -> bool:
        """
        Whether the bytes behind the digest can still be read.

        Args:
            digest (Digest): The content address to check.

        Returns:
            bool: Whether the bytes are present and not tombstoned.
        """
        return self._path_for(digest).exists() and digest.hex not in self._tombstones()

    def tombstone(self, digest: Digest, reason: str) -> None:
        """
        Destroy the bytes while keeping the identity.

        Args:
            digest (Digest): The content address to redact.
            reason (str): Why the bytes were destroyed, for the audit record.
        """
        self._path_for(digest).unlink(missing_ok=True)
        tombstones = dict(self._tombstones())
        tombstones[digest.hex] = reason
        self._write_tombstones(tombstones)

    def delete(self, digest: Digest) -> None:
        """
        Reclaim the bytes outright.

        Args:
            digest (Digest): The content address to reclaim.
        """
        self._path_for(digest).unlink(missing_ok=True)
        tombstones = dict(self._tombstones())
        if tombstones.pop(digest.hex, None) is not None:
            self._write_tombstones(tombstones)

    def iter_digests(self) -> Iterator[OciDigest]:
        """
        Iterate every resolvable digest held.

        Returns:
            Iterator[OciDigest]: The content addresses present on disk.
        """
        for path in sorted(self.blobs_dir.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                yield OciDigest.parse(f"{ALGORITHM}:{path.name}")

    def tombstoned(self) -> dict[OciDigest, str]:
        """
        The digests whose bytes were destroyed, and why.

        Returns:
            dict[OciDigest, str]: Redacted digests mapped to their recorded reason.
        """
        return {
            OciDigest.parse(f"{ALGORITHM}:{hex_digest}"): reason for hex_digest, reason in self._tombstones().items()
        }

    def read_pointer(self, name: str) -> bytes | None:
        """
        Read a named mutable pointer from the sidecar directory.

        Args:
            name (str): Pointer name.

        Returns:
            bytes | None: The stored value, or ``None`` if unset.
        """
        path = self.sidecar_dir / f"{name}.json"
        return path.read_bytes() if path.is_file() else None

    def write_pointer(self, name: str, data: bytes) -> None:
        """
        Set a named mutable pointer, writing atomically.

        Args:
            name (str): Pointer name.
            data (bytes): The value to store.
        """
        path = self.sidecar_dir / f"{name}.json"
        scratch = path.with_name(f".tmp-{path.name}")
        scratch.write_bytes(data)
        scratch.replace(path)

    # --- Sidecar state --------------------------------------------------------

    def _tombstones(self) -> dict[str, str]:
        if self._tombstone_cache is None:
            path = self.sidecar_dir / TOMBSTONES_FILE
            self._tombstone_cache = json.loads(path.read_text()) if path.is_file() else {}
        return self._tombstone_cache

    def _write_tombstones(self, tombstones: dict[str, str]) -> None:
        self._write_json(self.sidecar_dir / TOMBSTONES_FILE, tombstones)
        self._tombstone_cache = tombstones

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        scratch = path.with_name(f".tmp-{path.name}")
        scratch.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        scratch.replace(path)

    def __repr__(self) -> str:
        return f"OciLayoutStore({str(self.root)!r})"
