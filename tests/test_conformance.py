"""Run the conformance suite against both stores this SDK ships.

The two must be indistinguishable through the ``BlockStore`` interface. If they diverge, one
of them is wrong -- and a third-party store subclassing ``BlockStoreConformance`` the same way
would catch the same divergence in its own implementation.
"""

from pathlib import Path

import pytest

from boltzmann.conformance import (
    BlockStoreConformance,
    CompositionConformance,
    IdentityConformance,
    MerkleConformance,
)
from boltzmann.store.base import BlockStore
from boltzmann.store.memory import MemoryBlockStore
from boltzmann.store.oci_layout import OciLayoutStore


class TestIdentity(IdentityConformance):
    """The three levels of hashes and the canonical serialization."""


class TestMerkle(MerkleConformance):
    """The Merkle layout's required properties."""


class TestComposition(CompositionConformance):
    """How a version changes, and what refuses to."""


class TestMemoryStore(BlockStoreConformance):
    """The in-memory store."""

    def make_store(self) -> BlockStore:
        return MemoryBlockStore()


class TestOciLayoutStore(BlockStoreConformance):
    """The on-disk OCI layout store."""

    @pytest.fixture(autouse=True)
    def _tmp_root(self, tmp_path: Path) -> None:
        self._root = tmp_path / "brain"

    def make_store(self) -> BlockStore:
        return OciLayoutStore(self._root)
