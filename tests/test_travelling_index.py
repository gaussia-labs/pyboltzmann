"""The one derived structure that has to travel, and publishing a subset of modules.

Section 6.3 singles out the vector index: every other index is a deterministic function of the blocks,
so any client rebuilds it, but rebuilding this one needs an embedding model a model-agnostic client does
not carry. So it ships with its module and records what produced it.

Publishing a subset is the other half of the same question -- what an artifact is allowed to omit -- and
the answer is constrained by R1 rather than by convenience.
"""

import pickle
from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.distribution.media_types import (
    ANNOTATION_EMBEDDING_MODEL,
    ANNOTATION_INDEX_KIND,
    ANNOTATION_SOURCE_SNAPSHOT,
)
from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import BlockId
from boltzmann.indices.base import AbstractIndex, Index, IndexKind, TravellingIndex
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest

ALEX = Actor(id="alex", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REQUEST = RegistrationRequest(media_type="application/pdf", actor=ALEX)
REFERENCE = "registry.example/org/brain"


class FakeVectorIndex(AbstractIndex):
    """What an implementer supplies: not rebuildable, so it has to be publishable."""

    KIND = IndexKind.VECTOR
    REBUILDABLE = False

    def __init__(self, model: str = "qwen3-embedding@1.0") -> None:
        self.model = model
        self.vectors: dict[str, list[int]] = {}
        self.loads = 0

    @property
    def model_tag(self) -> str | None:
        return self.model

    def build(self, blocks) -> None:
        self.vectors = {block.block_id.hex: [len(block.label)] for block in blocks}

    def search(self, query, limit: int = 10):
        return []

    def dump(self) -> bytes:
        return pickle.dumps(self.vectors)

    def load(self, data: bytes) -> None:
        self.vectors = pickle.loads(data)
        self.loads += 1


class UndumpableIndex(AbstractIndex):
    """Claims it cannot be rebuilt but offers no way to publish it, which is a contradiction."""

    KIND = IndexKind.VECTOR
    REBUILDABLE = False

    def build(self, blocks) -> None:
        return None

    def search(self, query, limit: int = 10):
        return []


class RebuildableIndex(AbstractIndex):
    """A structural index. Any client regenerates it, so it must not travel."""

    KIND = IndexKind.HASH_MAP
    REBUILDABLE = True

    def __init__(self) -> None:
        self.count = 0

    def build(self, blocks) -> None:
        self.count = sum(1 for _ in blocks)

    def search(self, query, limit: int = 10):
        return []


def seed(brain: Brain, label: str = "Fourier") -> BlockId:
    source = brain.register(b"%PDF-1.7 lecture 07", REQUEST).block_id
    task = brain.define_task(source)
    brain.commit(
        brain.validate(
            CandidateSet(
                producer=MODEL,
                candidates=[
                    Candidate(
                        memory_type=MemoryType.SEMANTIC,
                        evidence=[source],
                        payload={"kind": "fact", "label": label, "statement": "about it"},
                    )
                ],
            ),
            task,
        )
    )
    return source


@pytest.fixture
def registry(tmp_path: Path) -> LocalLayoutRegistry:
    return LocalLayoutRegistry(tmp_path / "registry")


class TestTheInterface:
    def test_a_dumpable_index_satisfies_travelling(self) -> None:
        index = FakeVectorIndex()
        assert isinstance(index, TravellingIndex)
        assert isinstance(index, Index)

    def test_a_rebuildable_index_need_not_travel(self) -> None:
        index = RebuildableIndex()
        assert isinstance(index, Index)
        assert index.rebuildable
        assert not isinstance(index, TravellingIndex)

    def test_only_the_vector_index_is_declared_unrebuildable(self) -> None:
        """The paper names exactly one exception, and the reason is that it needs a model."""
        assert not FakeVectorIndex().rebuildable
        assert FakeVectorIndex().model_tag is not None


class TestPackingTheIndex:
    def test_an_unrebuildable_index_gets_its_own_layer(self, tmp_path: Path) -> None:
        index = FakeVectorIndex()
        brain = Brain.open(tmp_path / "brain", actor=ALEX, indices={MemoryType.SEMANTIC: [index]})
        seed(brain)

        manifest = brain.pack(tag="v1")
        layer = manifest.vector_index_for(MemoryType.SEMANTIC)
        assert layer is not None
        assert layer.is_vector_index
        assert layer.size > 0

    def test_the_layer_records_the_model_that_produced_it(self, tmp_path: Path) -> None:
        """So a consumer can tell whether what it received is comparable to what it could build."""
        index = FakeVectorIndex(model="some-embedder@3.1")
        brain = Brain.open(tmp_path / "brain", actor=ALEX, indices={MemoryType.SEMANTIC: [index]})
        seed(brain)

        layer = brain.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC)
        assert layer is not None
        assert layer.annotations[ANNOTATION_EMBEDDING_MODEL] == "some-embedder@3.1"
        assert layer.annotations[ANNOTATION_INDEX_KIND] == "vector"

    def test_it_does_not_shadow_the_module_layer(self, tmp_path: Path) -> None:
        index = FakeVectorIndex()
        brain = Brain.open(tmp_path / "brain", actor=ALEX, indices={MemoryType.SEMANTIC: [index]})
        seed(brain)

        manifest = brain.pack(tag="v1")
        module_layer = manifest.layer_for(MemoryType.SEMANTIC)
        index_layer = manifest.vector_index_for(MemoryType.SEMANTIC)
        assert module_layer is not None
        assert index_layer is not None
        assert module_layer.digest != index_layer.digest
        assert manifest.modules == [MemoryType.CANONICAL, MemoryType.SEMANTIC, MemoryType.PROVENANCE]

    def test_a_rebuildable_index_gets_no_layer(self, tmp_path: Path) -> None:
        """Shipping it would transfer bytes the consumer can regenerate for free."""
        brain = Brain.open(tmp_path / "brain", actor=ALEX, indices={MemoryType.SEMANTIC: [RebuildableIndex()]})
        seed(brain)
        assert brain.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC) is None

    def test_no_index_means_no_layer(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        manifest = brain.pack(tag="v1")
        assert all(not layer.is_vector_index for layer in manifest.layers)

    def test_an_unrebuildable_index_that_cannot_dump_is_refused(self, tmp_path: Path) -> None:
        """Otherwise the module would arrive without it and nothing could regenerate it."""
        brain = Brain.open(tmp_path / "brain", actor=ALEX, indices={MemoryType.SEMANTIC: [UndumpableIndex()]})
        seed(brain)
        with pytest.raises(DistributionError, match="cannot dump"):
            brain.pack(tag="v1")


class TestPullingTheIndex:
    async def test_a_consumer_receives_the_index(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        published = FakeVectorIndex()
        source = Brain.open(tmp_path / "a", actor=ALEX, indices={MemoryType.SEMANTIC: [published]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        received = FakeVectorIndex()
        target = Brain.open(tmp_path / "b", actor=ALEX, indices={MemoryType.SEMANTIC: [received]})
        await target.pull(registry, REFERENCE, "v1")

        assert received.loads == 1
        assert received.vectors == published.vectors
        assert target.verify()

    async def test_a_consumer_without_an_index_ignores_the_layer(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """A model-agnostic client that registered nothing must still be able to install the brain."""
        source = Brain.open(tmp_path / "a", actor=ALEX, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=ALEX)
        await target.pull(registry, REFERENCE, "v1")
        assert target.verify()
        assert target.root_of(MemoryType.SEMANTIC) == source.root_of(MemoryType.SEMANTIC)

    async def test_an_index_from_another_model_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Vectors from two models occupy different spaces, so mixing them would rank meaninglessly."""
        source = Brain.open(tmp_path / "a", actor=ALEX, indices={MemoryType.SEMANTIC: [FakeVectorIndex("qwen3@1.0")]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=ALEX, indices={MemoryType.SEMANTIC: [FakeVectorIndex("other@2.0")]})
        with pytest.raises(DistributionError, match="mix representation spaces"):
            await target.pull(registry, REFERENCE, "v1")

    async def test_a_selective_pull_of_another_module_skips_the_index(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        received = FakeVectorIndex()
        source = Brain.open(tmp_path / "a", actor=ALEX, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=ALEX, indices={MemoryType.SEMANTIC: [received]})
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.CANONICAL])
        assert received.loads == 0


class TestSelectivePublish:
    """What an artifact may omit is constrained by R1, not by convenience."""

    def test_a_derived_module_cannot_be_published_without_canonical(self, tmp_path: Path) -> None:
        """An artifact whose citations point nowhere could be trusted but never audited."""
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        with pytest.raises(DistributionError, match="without canonical"):
            brain.pack(tag="v1", modules=[MemoryType.SEMANTIC])

    def test_canonical_plus_a_derived_module_is_allowed(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        manifest = brain.pack(tag="lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        assert manifest.modules == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert manifest.layer_for(MemoryType.PROVENANCE) is None

    def test_canonical_alone_is_allowed(self, tmp_path: Path) -> None:
        """It cites nothing, so there is no citation to strand."""
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        assert brain.pack(tag="sources", modules=[MemoryType.CANONICAL]).modules == [MemoryType.CANONICAL]

    def test_an_uninstalled_module_cannot_be_published(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        with pytest.raises(DistributionError, match="not installed"):
            brain.pack(tag="v1", modules=[MemoryType.PROCEDURAL])

    def test_an_empty_artifact_is_refused(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        with pytest.raises(DistributionError, match="no modules"):
            brain.pack(tag="v1", modules=[])

    def test_the_config_carries_only_the_published_modules(self, tmp_path: Path) -> None:
        """A consumer's full pull adopts this document, so it has to describe what is actually there."""
        from boltzmann.module.snapshot import Snapshot

        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        manifest = brain.pack(tag="lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        projected = Snapshot.model_validate_json(brain.store.get_bytes(manifest.config.digest))
        assert projected.installed == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert projected.parent is None

    def test_a_projection_records_the_snapshot_it_came_from(self, tmp_path: Path) -> None:
        """A projection is in nobody's history, so the divergence check needs the real source."""
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        manifest = brain.pack(tag="lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        assert manifest.annotations[ANNOTATION_SOURCE_SNAPSHOT] == str(brain.snapshot().digest)
        assert manifest.config.digest != brain.snapshot().digest

    def test_a_complete_publish_projects_to_itself(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=ALEX)
        seed(brain)
        manifest = brain.pack(tag="v1")
        assert manifest.config.digest == brain.snapshot().digest
        assert manifest.annotations[ANNOTATION_SOURCE_SNAPSHOT] == str(brain.snapshot().digest)

    async def test_a_projection_can_be_pushed_and_installed(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        source = Brain.open(tmp_path / "a", actor=ALEX)
        seed(source)
        await source.push(registry, REFERENCE, "lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])

        target = Brain.open(tmp_path / "b", actor=ALEX)
        await target.pull(registry, REFERENCE, "lite")
        assert target.snapshot().installed == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert target.verify()
        assert target.root_of(MemoryType.SEMANTIC) == source.root_of(MemoryType.SEMANTIC)

    async def test_pushing_a_projection_twice_is_a_fast_forward(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The source annotation is what keeps this from looking like a divergence."""
        brain = Brain.open(tmp_path / "a", actor=ALEX)
        seed(brain)
        subset = [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        await brain.push(registry, REFERENCE, "lite", modules=subset)
        await brain.push(registry, REFERENCE, "lite", modules=subset)

    async def test_a_projection_and_the_full_brain_can_share_a_repository(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        brain = Brain.open(tmp_path / "a", actor=ALEX)
        seed(brain)
        await brain.push(registry, REFERENCE, "full")
        await brain.push(registry, REFERENCE, "lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])

        assert set(registry.tags(REFERENCE)) == {"full", "lite"}
        full = await registry.resolve(REFERENCE, "full")
        lite = await registry.resolve(REFERENCE, "lite")
        assert len(full.modules) > len(lite.modules)
