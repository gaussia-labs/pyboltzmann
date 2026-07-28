"""The on-disk store: the brain directory must be a valid OCI Image Layout.

The conformance suite already checks the behavior both stores share. This file checks the thing
that is specific to :class:`OciLayoutStore` and that the whole distribution design rests on:
that the local brain is already the published format, so publishing is a copy.
"""

import json
from pathlib import Path

import pytest

from boltzmann.blocks.semantic import SemanticBlock, SemanticKind
from boltzmann.exceptions import ModuleError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.store.oci_layout import IMAGE_INDEX_MEDIA_TYPE, IMAGE_LAYOUT_VERSION, OciLayoutStore


def block(label: str = "Fourier series") -> SemanticBlock:
    return SemanticBlock(kind=SemanticKind.FORMULA, label=label, statement="f(x) = ...")


class TestLayout:
    """The directory must satisfy the OCI Image Layout specification."""

    def test_creates_the_required_entries(self, tmp_path: Path) -> None:
        OciLayoutStore(tmp_path / "brain")
        root = tmp_path / "brain"
        assert (root / "oci-layout").is_file()
        assert (root / "index.json").is_file()
        assert (root / "blobs" / "sha256").is_dir()

    def test_declares_the_layout_version(self, tmp_path: Path) -> None:
        OciLayoutStore(tmp_path / "brain")
        marker = json.loads((tmp_path / "brain" / "oci-layout").read_text())
        assert marker == {"imageLayoutVersion": IMAGE_LAYOUT_VERSION}

    def test_index_starts_empty_and_well_formed(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        index = store.index()
        assert index["schemaVersion"] == 2
        assert index["mediaType"] == IMAGE_INDEX_MEDIA_TYPE
        assert index["manifests"] == []

    def test_reopening_preserves_content(self, tmp_path: Path) -> None:
        root = tmp_path / "brain"
        first = OciLayoutStore(root)
        block_id = first.put_block(block())
        assert OciLayoutStore(root, create=False).get_block(block_id) == block()

    def test_opening_a_non_layout_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "not-a-brain").mkdir()
        with pytest.raises(ModuleError, match="not an OCI layout"):
            OciLayoutStore(tmp_path / "not-a-brain", create=False)

    def test_wrong_layout_version_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "brain"
        OciLayoutStore(root)
        (root / "oci-layout").write_text(json.dumps({"imageLayoutVersion": "2.0.0"}))
        with pytest.raises(ModuleError, match="image layout version"):
            OciLayoutStore(root, create=False)


class TestBlobs:
    """Content addressing is the filesystem's deduplication."""

    def test_a_blob_is_one_file_named_by_its_digest(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        digest = store.put_bytes(b"%PDF-1.7 lecture notes")
        assert (store.blobs_dir / digest.hex).read_bytes() == b"%PDF-1.7 lecture notes"

    def test_identical_bytes_are_one_file(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        store.put_bytes(b"same")
        store.put_bytes(b"same")
        assert len(list(store.blobs_dir.iterdir())) == 1

    def test_a_block_and_its_source_are_separate_blobs(self, tmp_path: Path) -> None:
        """The canonical block describes the bytes; it is not the bytes."""
        store = OciLayoutStore(tmp_path / "brain")
        store.put_bytes(b"%PDF-1.7 lecture notes")
        store.put_block(block())
        assert len(list(store.blobs_dir.iterdir())) == 2

    def test_temporary_files_are_not_reported_as_blobs(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        store.put_block(block())
        (store.blobs_dir / ".tmp-leftover").write_bytes(b"interrupted write")
        assert all(not digest.hex.startswith(".") for digest in store.iter_digests())
        assert len(list(store.iter_digests())) == 1

    def test_writes_are_atomic(self, tmp_path: Path) -> None:
        """A reader never sees a partially written blob, so no scratch file survives."""
        store = OciLayoutStore(tmp_path / "brain")
        store.put_bytes(b"%PDF-1.7 lecture notes")
        assert not [path for path in store.blobs_dir.iterdir() if path.name.startswith(".tmp-")]


class TestSidecar:
    """Derived and non-content state lives outside blobs, because no root commits to it."""

    def test_tombstones_are_not_stored_as_content(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        block_id = store.put_block(block())
        store.tombstone(block_id, "erasure policy: personal data")
        assert (store.sidecar_dir / "tombstones.json").is_file()
        assert list(store.iter_digests()) == []

    def test_tombstones_survive_reopening(self, tmp_path: Path) -> None:
        root = tmp_path / "brain"
        store = OciLayoutStore(root)
        block_id = store.put_block(block())
        store.tombstone(block_id, "erasure policy: personal data")

        reopened = OciLayoutStore(root, create=False)
        assert reopened.has(block_id)
        assert not reopened.is_resolvable(block_id)
        assert reopened.tombstoned() == {OciDigest.parse(str(block_id)): "erasure policy: personal data"}

    def test_deleting_clears_the_tombstone(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        block_id = store.put_block(block())
        store.tombstone(block_id, "erasure policy")
        store.delete(block_id)
        assert not store.has(block_id)
        assert store.tombstoned() == {}

    def test_sidecar_is_separate_from_blobs(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        assert store.sidecar_dir.name == "boltzmann"
        assert store.blobs_dir.parent.name == "blobs"


class TestIntegrity:
    """A store must detect corruption rather than serve it."""

    def test_tampered_bytes_are_detected(self, tmp_path: Path) -> None:
        store = OciLayoutStore(tmp_path / "brain")
        block_id = store.put_block(block())
        (store.blobs_dir / block_id.hex).write_bytes(b'{"boltzmann":1,"tampered":true}')
        with pytest.raises(Exception, match=r"corrupt|canonical|malformed"):
            store.get_block(block_id)

    def test_resolution_is_level_agnostic(self, tmp_path: Path) -> None:
        """Physical resolution does not care what a digest means; the caller's type does."""
        store = OciLayoutStore(tmp_path / "brain")
        data = b"%PDF-1.7 lecture notes"
        digest = store.put_bytes(data)
        assert store.get_bytes(digest) == data
        assert store.get_bytes(BlockId.parse(str(digest))) == data
