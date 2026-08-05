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
from boltzmann.retention.policy import RetentionPolicy
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REQUEST = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
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

    def build(self, blocks, content) -> None:
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

    def build(self, blocks, content) -> None:
        return None

    def search(self, query, limit: int = 10):
        return []


class RebuildableIndex(AbstractIndex):
    """A structural index. Any client regenerates it, so it must not travel."""

    KIND = IndexKind.HASH_MAP
    REBUILDABLE = True

    def __init__(self) -> None:
        self.count = 0

    def build(self, blocks, content) -> None:
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
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})
        seed(brain)

        manifest = brain.pack(tag="v1")
        layer = manifest.vector_index_for(MemoryType.SEMANTIC)
        assert layer is not None
        assert layer.is_vector_index
        assert layer.size > 0

    def test_the_layer_records_the_model_that_produced_it(self, tmp_path: Path) -> None:
        """So a consumer can tell whether what it received is comparable to what it could build."""
        index = FakeVectorIndex(model="some-embedder@3.1")
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})
        seed(brain)

        layer = brain.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC)
        assert layer is not None
        assert layer.annotations[ANNOTATION_EMBEDDING_MODEL] == "some-embedder@3.1"
        assert layer.annotations[ANNOTATION_INDEX_KIND] == "vector"

    def test_it_does_not_shadow_the_module_layer(self, tmp_path: Path) -> None:
        index = FakeVectorIndex()
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})
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
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, indices={MemoryType.SEMANTIC: [RebuildableIndex()]})
        seed(brain)
        assert brain.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC) is None

    def test_no_index_means_no_layer(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        manifest = brain.pack(tag="v1")
        assert all(not layer.is_vector_index for layer in manifest.layers)

    def test_an_unrebuildable_index_that_cannot_dump_is_refused(self, tmp_path: Path) -> None:
        """Otherwise the module would arrive without it and nothing could regenerate it."""
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, indices={MemoryType.SEMANTIC: [UndumpableIndex()]})
        seed(brain)
        with pytest.raises(DistributionError, match="cannot dump"):
            brain.pack(tag="v1")


class TestPullingTheIndex:
    async def test_a_consumer_receives_the_index(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        published = FakeVectorIndex()
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [published]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        received = FakeVectorIndex()
        target = Brain.open(tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [received]})
        await target.pull(registry, REFERENCE, "v1")

        assert received.loads == 1
        assert received.vectors == published.vectors
        assert target.verify()

    async def test_a_consumer_without_an_index_ignores_the_layer(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """A model-agnostic client that registered nothing must still be able to install the brain."""
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR)
        await target.pull(registry, REFERENCE, "v1")
        assert target.verify()
        assert target.root_of(MemoryType.SEMANTIC) == source.root_of(MemoryType.SEMANTIC)

    async def test_an_index_from_another_model_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Vectors from two models occupy different spaces, so mixing them would rank meaninglessly."""
        source = Brain.open(
            tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex("qwen3@1.0")]}
        )
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(
            tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex("other@2.0")]}
        )
        with pytest.raises(DistributionError, match="mix representation spaces"):
            await target.pull(registry, REFERENCE, "v1")

    async def test_a_selective_pull_of_another_module_skips_the_index(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        received = FakeVectorIndex()
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [received]})
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.CANONICAL])
        assert received.loads == 0


class TestSelectivePublish:
    """What an artifact may omit is constrained by R1, not by convenience."""

    def test_a_derived_module_cannot_be_published_without_canonical(self, tmp_path: Path) -> None:
        """An artifact whose citations point nowhere could be trusted but never audited."""
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        with pytest.raises(DistributionError, match="without canonical"):
            brain.pack(tag="v1", modules=[MemoryType.SEMANTIC])

    def test_canonical_plus_a_derived_module_is_allowed(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        manifest = brain.pack(tag="lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        assert manifest.modules == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert manifest.layer_for(MemoryType.PROVENANCE) is None

    def test_canonical_alone_is_allowed(self, tmp_path: Path) -> None:
        """It cites nothing, so there is no citation to strand."""
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        assert brain.pack(tag="sources", modules=[MemoryType.CANONICAL]).modules == [MemoryType.CANONICAL]

    def test_an_uninstalled_module_cannot_be_published(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        with pytest.raises(DistributionError, match="not installed"):
            brain.pack(tag="v1", modules=[MemoryType.PROCEDURAL])

    def test_an_empty_artifact_is_refused(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        with pytest.raises(DistributionError, match="no modules"):
            brain.pack(tag="v1", modules=[])

    def test_the_config_carries_only_the_published_modules(self, tmp_path: Path) -> None:
        """A consumer's full pull adopts this document, so it has to describe what is actually there."""
        from boltzmann.module.snapshot import Snapshot

        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        manifest = brain.pack(tag="lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        projected = Snapshot.model_validate_json(brain.store.get_bytes(manifest.config.digest))
        assert projected.installed == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert projected.parent is None

    def test_a_projection_records_the_snapshot_it_came_from(self, tmp_path: Path) -> None:
        """A projection is in nobody's history, so the divergence check needs the real source."""
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        manifest = brain.pack(tag="lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        assert manifest.annotations[ANNOTATION_SOURCE_SNAPSHOT] == str(brain.snapshot().digest)
        assert manifest.config.digest != brain.snapshot().digest

    def test_a_complete_publish_projects_to_itself(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        seed(brain)
        manifest = brain.pack(tag="v1")
        assert manifest.config.digest == brain.snapshot().digest
        assert manifest.annotations[ANNOTATION_SOURCE_SNAPSHOT] == str(brain.snapshot().digest)

    async def test_a_projection_can_be_pushed_and_installed(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        source = Brain.open(tmp_path / "a", actor=CURATOR)
        seed(source)
        await source.push(registry, REFERENCE, "lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])

        target = Brain.open(tmp_path / "b", actor=CURATOR)
        await target.pull(registry, REFERENCE, "lite")
        assert target.snapshot().installed == [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        assert target.verify()
        assert target.root_of(MemoryType.SEMANTIC) == source.root_of(MemoryType.SEMANTIC)

    async def test_pushing_a_projection_twice_is_a_fast_forward(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The source annotation is what keeps this from looking like a divergence."""
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        seed(brain)
        subset = [MemoryType.CANONICAL, MemoryType.SEMANTIC]
        await brain.push(registry, REFERENCE, "lite", modules=subset)
        await brain.push(registry, REFERENCE, "lite", modules=subset)

    async def test_a_projection_and_the_full_brain_can_share_a_repository(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        seed(brain)
        await brain.push(registry, REFERENCE, "full")
        await brain.push(registry, REFERENCE, "lite", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])

        assert set(registry.tags(REFERENCE)) == {"full", "lite"}
        full = await registry.resolve(REFERENCE, "full")
        lite = await registry.resolve(REFERENCE, "lite")
        assert len(full.modules) > len(lite.modules)


class TestRebuildingWhatArrivedAnotherWay:
    """A structural index has to reflect the installed version on every path, not only after a write.

    The write path rebuilds what it touched, so an index is correct in the process that committed into it.
    That leaves two ways for a composition to arrive without a write -- reopening a brain, and installing
    a version -- and an index that is empty on those paths does not announce itself: a planner consulting
    it gets no candidates and reports a confident nothing.
    """

    def test_reopening_rebuilds_a_structural_index(self, tmp_path: Path) -> None:
        Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [RebuildableIndex()]})
        seed(Brain.open(tmp_path / "a", actor=CURATOR))

        index = RebuildableIndex()
        reopened = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})

        assert index.count == len(reopened.module(MemoryType.SEMANTIC).block_ids) == 1

    def test_reopening_does_not_rebuild_a_travelling_index(self, tmp_path: Path) -> None:
        """Regenerating it would replace what a peer published with whatever this client produced."""
        seed(Brain.open(tmp_path / "a", actor=CURATOR))

        travelling = FakeVectorIndex()
        Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [travelling]})

        assert travelling.vectors == {}

    async def test_installing_rebuilds_a_structural_index(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """What ``plan_pull`` reports under ``rebuild_indices``, actually done."""
        source = Brain.open(tmp_path / "a", actor=CURATOR)
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        index = RebuildableIndex()
        target = Brain.open(tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})
        plan = await target.plan_pull(registry, REFERENCE, "v1")
        assert IndexKind.HASH_MAP.value in plan.rebuild_indices

        await target.pull(registry, REFERENCE, "v1")
        assert index.count == 1

    async def test_installing_keeps_the_index_that_travelled(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The layer's vectors survive the rebuild that follows a pull."""
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")
        published = source.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR)

        landed = FakeVectorIndex()
        target = Brain.open(
            tmp_path / "b",
            actor=CURATOR,
            indices={MemoryType.SEMANTIC: [landed, RebuildableIndex()]},
        )
        await target.pull(registry, REFERENCE, "v1")

        assert landed.loads == 1
        assert landed.vectors == published.vectors

    def test_an_unresolvable_block_is_skipped_rather_than_raised(self, tmp_path: Path) -> None:
        """A block can be a member of a version and still not be readable, and an index reads.

        Redaction is the way to produce that state deliberately: the block stays in the composition and
        keeps proving into the root, but its bytes are gone. An index that insisted on reading it would
        make the brain impossible to reopen.
        """
        policy = RetentionPolicy(redactable_media_types=["application/pdf"])
        brain = Brain.open(tmp_path / "a", actor=CURATOR, policy=policy)
        source = seed(brain)
        brain.redact(source, MemoryType.CANONICAL, reason="unreadable now")

        index = RebuildableIndex()
        reopened = Brain.open(tmp_path / "a", actor=CURATOR, policy=policy, indices={MemoryType.CANONICAL: [index]})

        assert source in reopened.module(MemoryType.CANONICAL)
        assert index.count == 0

    def test_rebuilding_a_module_with_no_index_is_a_no_op(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        seed(brain)
        brain.rebuild_indices()
        brain.rebuild_indices([MemoryType.EPISODIC])


class TestPublishingOnlyWhatItCanVouchFor:
    """A travelling index cannot be regenerated, so an index this brain never populated holds nothing.

    Dumping it anyway publishes a layer that claims a vector index, carries none, and still names the model
    that produced it. The consumer loads it without error, holds nothing, and has no way to tell -- which is
    worse than no layer at all, because an absent layer is something ``plan_pull`` reports.
    """

    def test_an_empty_travelling_index_is_not_published(self, tmp_path: Path) -> None:
        seed(Brain.open(tmp_path / "a", actor=CURATOR))

        reopened = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        manifest = reopened.pack(tag="v1")

        assert manifest.vector_index_for(MemoryType.SEMANTIC) is None

    def test_an_index_built_by_a_write_is_published(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(brain)

        layer = brain.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC)
        assert layer is not None
        assert layer.annotations[ANNOTATION_EMBEDDING_MODEL] == "qwen3-embedding@1.0"

    async def test_an_index_loaded_from_a_layer_can_be_republished(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """A consumer that installed a brain is as entitled to publish it as the brain that built it."""
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        await target.pull(registry, REFERENCE, "v1")

        republished = target.pack(tag="v2").vector_index_for(MemoryType.SEMANTIC)
        original = source.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC)
        assert republished is not None
        assert original is not None
        assert republished.digest == original.digest


class TestSurvivingAReopen:
    """The index the layout already holds is the only one a reopened brain can get back."""

    def test_reopening_restores_a_packed_travelling_index(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(brain)
        brain.pack(tag="v1")
        published = brain.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR)

        landed = FakeVectorIndex()
        reopened = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [landed]})

        assert landed.vectors == published.vectors
        assert reopened.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC) is not None

    async def test_reopening_restores_an_installed_travelling_index(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The case the sandbox hit: ingest in one process, push from another, publish an empty index."""
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        await target.pull(registry, REFERENCE, "v1")

        landed = FakeVectorIndex()
        reopened = Brain.open(tmp_path / "b", actor=CURATOR, indices={MemoryType.SEMANTIC: [landed]})

        assert landed.loads == 1
        assert landed.vectors == source.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR).vectors
        assert reopened.verify()

    def test_a_manifest_for_another_version_is_not_used(self, tmp_path: Path) -> None:
        """Its index describes blocks this version may not have, and the digest of the config is what says
        which version a manifest belongs to."""
        brain = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(brain)
        brain.pack(tag="v1")
        seed(brain, label="Laplace")  # moves the snapshot on, without packing again

        landed = FakeVectorIndex()
        Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [landed]})
        assert landed.vectors == {}

    def test_a_reclaimed_index_layer_is_skipped(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(brain)
        layer = brain.pack(tag="v1").vector_index_for(MemoryType.SEMANTIC)
        assert layer is not None
        brain.store.delete(layer.digest)

        landed = FakeVectorIndex()
        Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [landed]})
        assert landed.vectors == {}

    def test_a_store_with_no_layout_index_opens_normally(self) -> None:
        brain = Brain(MemoryBlockStore(), actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(brain)
        assert brain.verify()


class TestOpeningIsNotInstalling:
    """A layer this client will not load must not make the brain unopenable.

    Refusing an index built by another embedding model is right on a pull -- the caller asked for that
    artifact. On open, the layout merely happens to hold one, and raising there strands the brain: every
    read, every write and every repack goes through opening it.
    """

    def test_an_index_from_another_model_does_not_block_opening(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(brain)
        brain.pack(tag="v1")

        class Newer(FakeVectorIndex):
            def __init__(self) -> None:
                super().__init__(model="qwen3-embedding@2.0")

        reopened = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [Newer()]})

        assert reopened.verify()
        assert MemoryType.SEMANTIC not in reopened.travelling_indices
        assert reopened.pack(tag="v2").vector_index_for(MemoryType.SEMANTIC) is None

    async def test_a_pull_still_refuses_it(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Installing an artifact is a request, and mixing representation spaces is not what was asked."""
        source = Brain.open(tmp_path / "a", actor=CURATOR, indices={MemoryType.SEMANTIC: [FakeVectorIndex()]})
        seed(source)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(
            tmp_path / "b",
            actor=CURATOR,
            indices={MemoryType.SEMANTIC: [FakeVectorIndex(model="qwen3-embedding@2.0")]},
        )
        with pytest.raises(DistributionError, match="representation spaces"):
            await target.pull(registry, REFERENCE, "v1")
