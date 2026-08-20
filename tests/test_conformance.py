"""Run the conformance suite against both stores this SDK ships.

The two must be indistinguishable through the ``BlockStore`` interface. If they diverge, one
of them is wrong -- and a third-party store subclassing ``BlockStoreConformance`` the same way
would catch the same divergence in its own implementation.
"""

from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.conformance import (
    BlockStoreConformance,
    BrainReaderConformance,
    CompositionConformance,
    IdentityConformance,
    MerkleConformance,
    ReconciliationConformance,
)
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
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


class TestBrainAsReader(BrainReaderConformance):
    """The SDK's own client, run against the contract a third-party client must satisfy.

    Running it here is what makes the suite trustworthy: a suite nobody passes is not a specification,
    it is a wish.
    """

    def make_reader(self) -> Brain:
        actor = Actor(id="conformance", kind=ActorKind.HUMAN)
        brain = Brain(MemoryBlockStore(), actor=actor)
        source = brain.register(
            b"%PDF-1.7 lecture notes on Fourier analysis",
            RegistrationRequest(media_type="application/pdf", actor=actor),
        ).block_id
        task = brain.define_task(source, allowed=[MemoryType.SEMANTIC])
        brain.commit(
            brain.validate(
                CandidateSet(
                    producer=Producer(kind=ProducerKind.MODEL, id="conformance-model", version="1"),
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.SEMANTIC,
                            evidence=[source],
                            payload={
                                "kind": "formula",
                                "label": "Fourier series",
                                "statement": "decomposes a periodic function into sines",
                                "subject": "signals",
                            },
                        )
                    ],
                ),
                task,
            )
        )
        return brain


class TestReconciliation(ReconciliationConformance):
    """Set arithmetic over immutable blocks, and the lineage that records it."""
