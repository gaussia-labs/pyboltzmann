"""Distribution end to end: pull, modify locally, push back.

The transport under test is :class:`LocalLayoutRegistry`, because OCI layouts are a first-class
transport target and it exercises the same code path a network registry would. The network client is
tested against a fake registry, since a real one is not available offline.
"""

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain, Origin
from boltzmann.distribution.layers import (
    SNAPSHOT_PREFIX,
    pack_history,
    pack_module,
    required_blobs,
    unpack_history,
    unpack_layer,
)
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.distribution.media_types import (
    ANNOTATION_SNAPSHOT_COUNT,
    ARTIFACT_TYPE,
    REF_NAME_ANNOTATION,
    memory_type_of,
)
from boltzmann.exceptions import DistributionError, ReferenceNotFoundError
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
SAM = Actor(id="sam", kind=ActorKind.AGENT)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REFERENCE = "registry.example/org/brain"


def llm(label: str):
    def propose(task, source: bytes) -> CandidateSet:
        return CandidateSet(
            producer=MODEL,
            candidates=[
                Candidate(
                    memory_type=MemoryType.SEMANTIC,
                    evidence=[task.source],
                    payload={"kind": "formula", "label": label, "statement": f"about {label}"},
                )
            ],
        )

    return propose


@pytest.fixture
def registry(tmp_path: Path) -> LocalLayoutRegistry:
    return LocalLayoutRegistry(tmp_path / "registry")


@pytest.fixture
def request_() -> RegistrationRequest:
    return RegistrationRequest(media_type="application/pdf", actor=CURATOR)


def seeded(path: Path, request_: RegistrationRequest, label: str = "Fourier") -> Brain:
    brain = Brain.open(path, actor=CURATOR)
    brain.ingest(b"%PDF-1.7 Lecture 07", request_, llm(label))
    return brain


class TestLayers:
    """A layer holds one module, and holds everything that module needs."""

    def test_packing_is_deterministic(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """Two clients packing the same composition must get the same digest, or dedup stops working."""
        brain = seeded(tmp_path / "brain", request_)
        module = brain.module(MemoryType.SEMANTIC)
        assert pack_module(module) == pack_module(module)

    def test_a_canonical_layer_carries_the_observed_bytes(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """Otherwise it arrives as claims about evidence the consumer cannot read."""
        brain = seeded(tmp_path / "brain", request_)
        canonical = brain.module(MemoryType.CANONICAL)
        block = canonical.get(canonical.block_ids[0])
        assert block.blob in required_blobs(canonical)

    def test_a_derived_layer_carries_only_its_blocks(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        semantic = seeded(tmp_path / "brain", request_).module(MemoryType.SEMANTIC)
        assert required_blobs(semantic) == semantic.block_ids

    def test_round_trips_into_a_fresh_store(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        module = seeded(tmp_path / "brain", request_).module(MemoryType.SEMANTIC)
        target = MemoryBlockStore()
        recovered = unpack_layer(pack_module(module), target)
        assert recovered == module.composition
        assert recovered.root == module.root

    def test_the_composition_document_lands_as_a_blob(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """The snapshot names it by digest, so unpacking has to store it, not just read it."""
        brain = seeded(tmp_path / "brain", request_)
        module = brain.module(MemoryType.SEMANTIC)
        target = MemoryBlockStore()
        unpack_layer(pack_module(module), target)
        assert target.has(brain.snapshot().modules[MemoryType.SEMANTIC].composition)

    def test_a_corrupt_layer_is_refused(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        module = seeded(tmp_path / "brain", request_).module(MemoryType.SEMANTIC)
        packed = bytearray(pack_module(module))
        packed[-20:] = b"\x00" * 20
        with pytest.raises(DistributionError):
            unpack_layer(bytes(packed), MemoryBlockStore())

    def test_a_layer_without_its_composition_is_refused(self) -> None:
        import gzip
        import io
        import tarfile

        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as archive:
            info = tarfile.TarInfo(name="blobs/deadbeef")
            info.size = 0
            archive.addfile(info, io.BytesIO(b""))
        with pytest.raises(DistributionError, match=r"carries no composition\.json"):
            unpack_layer(gzip.compress(raw.getvalue()), MemoryBlockStore())


class TestPack:
    """Packing locally turns the layout into a consumable artifact, with no network involved."""

    def test_writes_a_manifest_into_the_index(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        brain = seeded(tmp_path / "brain", request_)
        assert brain.store.index()["manifests"] == []

        manifest = brain.pack(tag="v1")
        index = brain.store.index()
        assert len(index["manifests"]) == 1
        entry = index["manifests"][0]
        assert entry["artifactType"] == ARTIFACT_TYPE
        assert entry["annotations"][REF_NAME_ANNOTATION] == "v1"
        assert entry["digest"] == str(manifest.digest)

    def test_one_layer_per_installed_module(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        manifest = seeded(tmp_path / "brain", request_).pack(tag="v1")
        assert {memory_type_of(layer.media_type) for layer in manifest.layers if not layer.is_history} == {
            MemoryType.CANONICAL,
            MemoryType.SEMANTIC,
            MemoryType.PROVENANCE,
        }

    def test_the_history_travels_as_its_own_layer(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """A snapshot names its parents, so an artifact that published only its head would name documents
        the consumer cannot resolve."""
        brain = seeded(tmp_path / "brain", request_)
        manifest = brain.pack(tag="v1")

        history = manifest.history
        assert history is not None
        assert history.annotations[ANNOTATION_SNAPSHOT_COUNT] == str(len(brain.reachable_history()))
        assert manifest.modules == brain.snapshot().installed  # the history layer is not a module

    def test_a_history_layer_that_misnames_its_entries_is_refused(
        self, tmp_path: Path, request_: RegistrationRequest
    ) -> None:
        """Content addressing means the name cannot make a substituted document land under the digest a
        lineage asks for -- it lands under its own, and the parent simply fails to resolve much later. A
        producer whose naming and payloads disagree is malformed, and one refusal here beats an unexplained
        "no common ancestor" at reconcile time.
        """
        brain = seeded(tmp_path / "brain", request_)
        documents = [brain.store.get_bytes(digest) for digest in brain.reachable_history()]
        layer = pack_history(documents)

        raw = gzip.decompress(layer)
        renamed = io.BytesIO()
        with (
            tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source,
            tarfile.open(fileobj=renamed, mode="w", format=tarfile.PAX_FORMAT) as target,
        ):
            for info in source.getmembers():
                handle = source.extractfile(info)
                assert handle is not None
                payload = handle.read()
                info.name = f"{SNAPSHOT_PREFIX}{'0' * 64}"
                target.addfile(info, io.BytesIO(payload))
        tampered = gzip.compress(renamed.getvalue())

        with pytest.raises(DistributionError, match="naming and its payloads disagree"):
            unpack_history(tampered, MemoryBlockStore())

    def test_a_history_layer_round_trips(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        brain = seeded(tmp_path / "brain", request_)
        expected = set(brain.reachable_history())
        store = MemoryBlockStore()

        assert set(unpack_history(pack_history([brain.store.get_bytes(d) for d in expected]), store)) == expected

    def test_each_layer_carries_its_modules_root(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """The descriptor's digest names the file; the annotation names the composition inside it."""
        brain = seeded(tmp_path / "brain", request_)
        manifest = brain.pack(tag="v1")
        for memory_type in brain.snapshot().installed:
            layer = manifest.layer_for(memory_type)
            assert layer is not None
            assert layer.merkle_root == brain.root_of(memory_type)
            assert layer.digest != layer.merkle_root

    def test_repacking_the_same_tag_replaces_the_entry(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        brain = seeded(tmp_path / "brain", request_)
        brain.pack(tag="v1")
        brain.pack(tag="v1")
        assert len(brain.store.index()["manifests"]) == 1


class TestPushAndPull:
    """The cycle: publish, install elsewhere, and get the same verified knowledge."""

    async def test_a_full_round_trip(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        source = seeded(tmp_path / "a", request_)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")

        assert target.snapshot().installed == source.snapshot().installed
        for memory_type in source.snapshot().installed:
            assert target.root_of(memory_type) == source.root_of(memory_type)
        assert target.verify()

    async def test_pushing_records_the_tag(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        brain = seeded(tmp_path / "a", request_)
        await brain.push(registry, REFERENCE, "v1")
        assert registry.tags(REFERENCE) == ["v1"]

    async def test_pushing_without_a_reference_is_refused(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        brain = seeded(tmp_path / "a", request_)
        with pytest.raises(DistributionError, match="no repository to push to"):
            await brain.push(registry)

    async def test_pushing_an_empty_brain_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        brain = Brain.open(tmp_path / "empty", actor=CURATOR)
        with pytest.raises(DistributionError, match="no snapshot to publish"):
            await brain.push(registry, REFERENCE, "v1")

    async def test_an_unpublished_tag_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Narrowly, so that a caller can tell absence from a transport it could not read at all."""
        brain = Brain.open(tmp_path / "b", actor=SAM)
        with pytest.raises(ReferenceNotFoundError, match="not published"):
            await brain.pull(registry, REFERENCE, "v1")

    async def test_a_wrong_tag_lists_what_exists(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        brain = Brain.open(tmp_path / "b", actor=SAM)
        with pytest.raises(DistributionError, match="published tags: v1"):
            await brain.pull(registry, REFERENCE, "v99")


class TestSelectiveInstallation:
    """Installing one module must not require the rest (paper Section 7.2)."""

    async def test_pulls_only_the_wanted_layer(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        source = seeded(tmp_path / "a", request_)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.SEMANTIC])

        assert target.snapshot().installed == [MemoryType.SEMANTIC]
        assert target.root_of(MemoryType.SEMANTIC) == source.root_of(MemoryType.SEMANTIC)

        held = len(list((tmp_path / "b" / "blobs" / "sha256").iterdir()))
        complete = len(list((tmp_path / "a" / "blobs" / "sha256").iterdir()))
        assert held < complete

    async def test_a_partial_install_still_verifies_and_proves(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """Each module verifies against its own root, so canonical is not needed to prove membership."""
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.SEMANTIC])

        assert target.verify()
        block_id = target.module(MemoryType.SEMANTIC).block_ids[0]
        assert target.prove(block_id, MemoryType.SEMANTIC).verify(target.root_of(MemoryType.SEMANTIC))

    async def test_a_partial_install_cannot_derive_new_knowledge(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """Writing derived blocks needs the canonical evidence, so the gate has something to check."""
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.SEMANTIC])

        with pytest.raises(Exception, match="not in the canonical composition"):
            target.define_task(target.module(MemoryType.SEMANTIC).block_ids[0])

    async def test_asking_for_a_module_the_artifact_lacks_is_refused(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        with pytest.raises(DistributionError, match="does not carry procedural"):
            await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.PROCEDURAL])


class TestIncrementalUpdate:
    """An update transfers what changed, and reuses the rest by digest (paper Section 7.3)."""

    async def test_an_unchanged_module_keeps_its_digest(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        source = brain.register(b"%PDF-1.7 Lecture 07", request_).block_id
        task = brain.define_task(source)
        brain.commit(brain.validate(llm("A")(task, b""), task))
        await brain.push(registry, REFERENCE, "v7")

        # A second commit that adds no evidence: canonical cannot have changed.
        brain.commit(brain.validate(llm("B")(task, b""), task))
        await brain.push(registry, tag="v8")

        v7 = await registry.resolve(REFERENCE, "v7")
        v8 = await registry.resolve(REFERENCE, "v8")
        canonical_v7 = v7.layer_for(MemoryType.CANONICAL)
        canonical_v8 = v8.layer_for(MemoryType.CANONICAL)
        assert canonical_v7 is not None
        assert canonical_v8 is not None
        assert canonical_v7.digest == canonical_v8.digest

        semantic_v7 = v7.layer_for(MemoryType.SEMANTIC)
        semantic_v8 = v8.layer_for(MemoryType.SEMANTIC)
        assert semantic_v7 is not None
        assert semantic_v8 is not None
        assert semantic_v7.digest != semantic_v8.digest

    async def test_pulling_an_update_reuses_held_layers(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        source = brain.register(b"%PDF-1.7 Lecture 07", request_).block_id
        task = brain.define_task(source)
        brain.commit(brain.validate(llm("A")(task, b""), task))
        await brain.push(registry, REFERENCE, "v7")

        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v7")
        after_first = len(list((tmp_path / "b" / "blobs" / "sha256").iterdir()))

        brain.commit(brain.validate(llm("B")(task, b""), task))
        await brain.push(registry, tag="v8")
        await target.pull(registry, REFERENCE, "v8")

        assert target.root_of(MemoryType.SEMANTIC) == brain.root_of(MemoryType.SEMANTIC)
        assert len(target.module(MemoryType.SEMANTIC)) == 2
        # Some blobs were added, but the canonical layer was not re-fetched twice.
        assert len(list((tmp_path / "b" / "blobs" / "sha256").iterdir())) > after_first
        assert target.verify()


class TestDivergence:
    """A push must not drop a remote snapshot this brain does not contain."""

    async def test_a_diverged_push_is_refused(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        first = seeded(tmp_path / "a", request_)
        await first.push(registry, REFERENCE, "v1")

        second = Brain.open(tmp_path / "b", actor=SAM)
        await second.pull(registry, REFERENCE, "v1")
        source = second.module(MemoryType.CANONICAL).block_ids[0]
        task = second.define_task(source)
        second.commit(second.validate(llm("B")(task, b""), task))
        await second.push(registry, tag="v2")

        with pytest.raises(DistributionError, match="the two diverged"):
            await first.push(registry, tag="v2")

    async def test_force_overwrites_a_diverged_remote(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        first = seeded(tmp_path / "a", request_)
        await first.push(registry, REFERENCE, "v1")

        second = Brain.open(tmp_path / "b", actor=SAM)
        await second.pull(registry, REFERENCE, "v1")
        source = second.module(MemoryType.CANONICAL).block_ids[0]
        task = second.define_task(source)
        second.commit(second.validate(llm("B")(task, b""), task))
        await second.push(registry, tag="v2")

        digest = await first.push(registry, tag="v2", force=True)
        assert (await registry.resolve(REFERENCE, "v2")).digest == digest

    async def test_pulling_then_pushing_succeeds(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """The prescribed resolution: pull, re-commit, push."""
        first = seeded(tmp_path / "a", request_)
        await first.push(registry, REFERENCE, "v1")

        second = Brain.open(tmp_path / "b", actor=SAM)
        await second.pull(registry, REFERENCE, "v1")
        source = second.module(MemoryType.CANONICAL).block_ids[0]
        task = second.define_task(source)
        second.commit(second.validate(llm("B")(task, b""), task))
        await second.push(registry, tag="v2")

        await first.pull(registry, REFERENCE, "v2")
        task = first.define_task(first.module(MemoryType.CANONICAL).block_ids[0])
        first.commit(first.validate(llm("C")(task, b""), task))
        await first.push(registry, tag="v3")

        assert registry.tags(REFERENCE) == ["v1", "v2", "v3"]
        assert len(first.module(MemoryType.SEMANTIC)) == 3

    async def test_pulling_then_pushing_to_the_same_tag_is_a_fast_forward(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """A full pull adopts the remote document verbatim, so its digest is in the local ancestry."""
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")
        assert target.snapshot().digest == (await registry.resolve(REFERENCE, "v1")).config.digest

        source = target.module(MemoryType.CANONICAL).block_ids[0]
        task = target.define_task(source)
        target.commit(target.validate(llm("B")(task, b""), task))
        await target.push(registry, tag="v1")  # same tag, must not look like a divergence

    async def test_republishing_a_partial_install_over_its_tag_is_refused(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """It would silently drop the modules that were never fetched."""
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.SEMANTIC])

        assert target.origin is not None
        assert target.origin.partial
        with pytest.raises(DistributionError, match="which this snapshot does not name"):
            await target.push(registry, tag="v1")

    async def test_a_partial_install_may_be_published_elsewhere(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """A semantic-only brain is a legitimate artifact; only overwriting its source is refused."""
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.SEMANTIC])

        await target.push(registry, "registry.example/org/semantic-only", "v1")
        pulled = await registry.resolve("registry.example/org/semantic-only", "v1")
        assert pulled.modules == [MemoryType.SEMANTIC]

    async def test_re_pushing_the_same_snapshot_is_fine(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        """The remote is exactly this brain's snapshot, so it is an ancestor of itself."""
        brain = seeded(tmp_path / "a", request_)
        await brain.push(registry, REFERENCE, "v1")
        await brain.push(registry, tag="v1")

    async def test_a_new_tag_needs_no_check(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        brain = seeded(tmp_path / "a", request_)
        await brain.push(registry, REFERENCE, "v1")
        await brain.push(registry, tag="totally-new")
        assert set(registry.tags(REFERENCE)) == {"v1", "totally-new"}


class TestOrigin:
    """A local brain remembers where it came from, like a tracking branch."""

    async def test_pull_records_the_origin(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")

        assert target.origin == Origin(
            reference=REFERENCE,
            tag="v1",
            snapshot=(await registry.resolve(REFERENCE, "v1")).config.digest,
        )

    async def test_the_origin_survives_reopening(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")

        reopened = Brain.open(tmp_path / "b", actor=SAM)
        assert reopened.origin == target.origin
        assert "origin" in reopened.state()

    async def test_a_commit_keeps_the_origin(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        await seeded(tmp_path / "a", request_).push(registry, REFERENCE, "v1")
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")

        source = target.module(MemoryType.CANONICAL).block_ids[0]
        task = target.define_task(source)
        target.commit(target.validate(llm("B")(task, b""), task))
        assert target.origin is not None
        assert target.origin.reference == REFERENCE

    async def test_push_lets_the_origin_be_omitted(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        brain = seeded(tmp_path / "a", request_)
        await brain.push(registry, REFERENCE, "v1")
        assert brain.origin is not None
        await brain.push(registry)  # no reference, no tag
        assert registry.tags(REFERENCE) == ["v1"]


class TestAncestry:
    """What the fast-forward check walks."""

    def test_walks_the_parent_chain(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        source = brain.register(b"%PDF-1.7 Lecture 07", request_).block_id
        task = brain.define_task(source)
        brain.commit(brain.validate(llm("A")(task, b""), task))

        ancestry = brain.ancestry()
        assert ancestry[0] == brain.snapshot().digest
        assert len(ancestry) == 2
        assert all(brain.store.is_resolvable(digest) for digest in ancestry)

    def test_the_first_version_has_no_parent(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """The empty snapshot a fresh handle starts from is a placeholder, not a published version."""
        brain = Brain.open(tmp_path / "a", actor=CURATOR)
        brain.register(b"%PDF-1.7 Lecture 07", request_)
        assert brain.snapshot().parents == []
        assert brain.ancestry() == [brain.snapshot().digest]

    def test_an_empty_brain_has_no_ancestry(self, tmp_path: Path) -> None:
        assert Brain.open(tmp_path / "empty", actor=CURATOR).ancestry() == []
