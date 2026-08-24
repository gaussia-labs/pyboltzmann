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


class TestSignedDistribution:
    """Signatures accumulate around an artifact, never inside it, and travel wherever it goes."""

    def _party(self, seed: int):
        ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
        from boltzmann.authenticity import SshPublicKey, rfc4253_signature

        private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        line = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)

        class Party:
            public_key = SshPublicKey.parse(line.decode("ascii"))

            @staticmethod
            def sign_blob(data: bytes) -> bytes:
                return rfc4253_signature("ssh-ed25519", private.sign(data))

        return Party()

    def _governed(self, path: Path, party) -> Brain:
        from boltzmann.authenticity import Scope, TrustedKey, TrustRoot

        root = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(
                TrustedKey(
                    key=party.public_key,
                    scopes=(Scope.INGEST, Scope.COMMIT, Scope.GOVERN),
                    since=1,
                ),
            ),
        )
        return Brain.init(path, actor=CURATOR, trust_root=root, signers=[party])

    def test_countersigning_leaves_the_brain_manifest_digest_untouched(self, tmp_path: Path) -> None:
        # The test the whole referrers design exists for: a brain must not change identity
        # because someone agreed with it.
        party = self._party(0x71)
        second = self._party(0x72)
        brain = self._governed(tmp_path / "brain", party)
        manifest = brain.pack(tag="v1")
        before = manifest.digest

        record = brain.countersign(brain.snapshot().canonical_bytes(), second)
        brain.add_signature(record)
        after = brain.pack(tag="v1")
        assert after.digest == before

    def test_the_manifest_declares_the_trust_root_before_any_transfer(self, tmp_path: Path) -> None:
        from boltzmann.distribution.media_types import ANNOTATION_TRUST_ROOT

        party = self._party(0x73)
        brain = self._governed(tmp_path / "brain", party)
        manifest = brain.pack(tag="v1")
        assert manifest.annotations[ANNOTATION_TRUST_ROOT] == str(brain.trust_root.digest)  # type: ignore[union-attr]

    async def test_signatures_travel_and_the_consumer_verifies_them(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        from boltzmann.authenticity import AuthorshipState, PinSource

        party = self._party(0x74)
        publisher = self._governed(tmp_path / "publisher", party)
        await publisher.push(registry, REFERENCE, "v1")

        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        installed = await consumer.pull(registry, REFERENCE, "v1")
        assert consumer.signatures(installed.digest), "the records crossed with the artifact"
        report = consumer.authenticate()
        assert report.state is AuthorshipState.AUTHORIZED

        # Trust on first use: pin, then pull again -- the anchor holds.
        pin = consumer.pin()
        assert pin.source is PinSource.FIRST_USE
        await consumer.pull(registry, REFERENCE, "v1")

    async def test_a_pinned_consumer_refuses_a_swapped_authority_before_any_layer_moves(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        from boltzmann.exceptions import TrustRootMismatchError

        party = self._party(0x75)
        mallory = self._party(0x76)
        publisher = self._governed(tmp_path / "publisher", party)
        await publisher.push(registry, REFERENCE, "v1")

        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        await consumer.pull(registry, REFERENCE, "v1")
        consumer.pin()

        # Mallory republishes the tag with their own, internally flawless brain.
        forged = self._governed(tmp_path / "mallory", mallory)
        await forged.push(registry, REFERENCE, "v1", force=True)

        with pytest.raises(TrustRootMismatchError, match="before transferring"):
            await consumer.pull(registry, REFERENCE, "v1")

    async def test_an_approved_rotation_passes_the_pin_gate(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        from boltzmann.authenticity import AuthorshipState, Scope, TrustedKey, TrustRoot

        party = self._party(0x77)
        second = self._party(0x78)
        publisher = self._governed(tmp_path / "publisher", party)
        await publisher.push(registry, REFERENCE, "v1")

        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        await consumer.pull(registry, REFERENCE, "v1")
        consumer.pin()

        revised = TrustRoot(
            revision=2,
            govern_quorum=1,
            keys=(
                *publisher.trust_root.keys,  # type: ignore[union-attr]
                TrustedKey(key=second.public_key, scopes=(Scope.COMMIT,), since=2),
            ),
        )
        publisher.rotate(revised, signers=[party])
        await publisher.push(registry, REFERENCE, "v1")

        installed = await consumer.pull(registry, REFERENCE, "v1")
        assert installed.trust_root == revised
        assert consumer.authenticate().state is AuthorshipState.AUTHORIZED

    async def test_a_transport_without_referrers_still_moves_the_brain(
        self, tmp_path: Path, registry: LocalLayoutRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        class BareTransport:
            """A third-party RegistryClient that never learned about referrers."""

            async def resolve(self, reference: str, tag: str):
                return await registry.resolve(reference, tag)

            async def pull_blob(self, reference: str, digest, store) -> None:
                await registry.pull_blob(reference, digest, store)

            async def push(self, reference: str, tag: str, manifest, store):
                return await registry.push(reference, tag, manifest, store)

        party = self._party(0x79)
        publisher = self._governed(tmp_path / "publisher", party)
        with caplog.at_level("WARNING"):
            await publisher.push(BareTransport(), REFERENCE, "v1")
        assert any("stay local" in message for message in caplog.messages)

        from boltzmann.authenticity import AuthorshipState

        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        await consumer.pull(BareTransport(), REFERENCE, "v1")
        assert consumer.authenticate().state is AuthorshipState.UNSIGNED

    def test_prune_keeps_the_records_and_the_empty_config(self, tmp_path: Path) -> None:
        party = self._party(0x7A)
        brain = self._governed(tmp_path / "brain", party)
        brain.pack(tag="v1")
        report = brain.prune(dry_run=False)
        assert brain.signatures(), "pruning must never reclaim a live signature"
        assert brain.authenticate().state.value == "authorized"
        assert report.reclaimed == []

    def test_a_signature_manifest_parses_and_a_foreign_one_is_refused(self, tmp_path: Path) -> None:
        from boltzmann.distribution.manifest import parse_signature_manifest

        party = self._party(0x7B)
        brain = self._governed(tmp_path / "brain", party)
        manifest = brain.pack(tag="v1")
        index = brain.store.index()  # type: ignore[attr-defined]
        signature_entries = [
            entry
            for entry in index["manifests"]
            if entry.get("artifactType") == "application/vnd.gaussia.boltzmann.signature.v1+json"
        ]
        assert len(signature_entries) == 1
        from boltzmann.identity.digest import OciDigest

        parsed = parse_signature_manifest(brain.store.get_bytes(OciDigest.parse(signature_entries[0]["digest"])))
        assert parsed.subject.digest == manifest.digest
        with pytest.raises(DistributionError, match="not a Boltzmann signature"):
            parse_signature_manifest(manifest.to_bytes())


class TestVerificationPolicyOnPull:
    """The install-time gate: unsigned brains, stripped signatures, and propose-scoped heads."""

    def _party(self, seed: int):
        ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
        from boltzmann.authenticity import SshPublicKey, rfc4253_signature

        private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        line = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)

        class Party:
            public_key = SshPublicKey.parse(line.decode("ascii"))

            @staticmethod
            def sign_blob(data: bytes) -> bytes:
                return rfc4253_signature("ssh-ed25519", private.sign(data))

        return Party()

    async def test_an_unsigned_brain_installs_with_a_warning_on_first_contact(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest, caplog
    ) -> None:
        publisher = seeded(tmp_path / "publisher", request_)
        await publisher.push(registry, REFERENCE, "v1")
        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        with caplog.at_level("WARNING"):
            await consumer.pull(registry, REFERENCE, "v1")
        assert any("authorship is unclaimed" in message for message in caplog.messages)

    async def test_a_stripped_brain_is_refused_once_seen_signed(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        from boltzmann.authenticity import Scope, TrustedKey, TrustRoot, UnsignedPolicy, VerificationPolicy
        from boltzmann.exceptions import UnsignedBrainError

        party = self._party(0x81)
        root = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(TrustedKey(key=party.public_key, scopes=(Scope.INGEST, Scope.COMMIT, Scope.GOVERN), since=1),),
        )
        publisher = Brain.init(tmp_path / "publisher", actor=CURATOR, trust_root=root, signers=[party])
        await publisher.push(registry, REFERENCE, "v1")
        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        await consumer.pull(registry, REFERENCE, "v1")

        # A new version whose signature never arrives: from this consumer's seat, stripping.
        publisher.ingest(b"%PDF-1.7 more evidence", request_, llm("Laplace"))
        await publisher.push(registry, REFERENCE, "v2")
        with pytest.raises(UnsignedBrainError, match="stripping"):
            await consumer.pull(registry, REFERENCE, "v2")
        # And the policy is the consumer's to relax, explicitly.
        await consumer.pull(registry, REFERENCE, "v2", verification=VerificationPolicy(unsigned=UnsignedPolicy.PERMIT))

    async def test_a_propose_scoped_head_is_refused_unless_the_policy_permits_it(
        self, tmp_path: Path, registry: LocalLayoutRegistry, request_: RegistrationRequest
    ) -> None:
        from boltzmann.authenticity import Scope, TrustedKey, TrustRoot, VerificationPolicy
        from boltzmann.exceptions import InsufficientScopeError

        owner = self._party(0x82)
        contributor = self._party(0x83)
        root = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(
                TrustedKey(key=owner.public_key, scopes=(Scope.INGEST, Scope.COMMIT, Scope.GOVERN), since=1),
                TrustedKey(key=contributor.public_key, scopes=(Scope.PROPOSE,), since=1),
            ),
        )
        publisher = Brain.init(tmp_path / "publisher", actor=CURATOR, trust_root=root, signers=[owner])
        publisher.ingest(b"%PDF-1.7 contributed evidence", request_, llm("Fourier"))
        source = publisher.module(MemoryType.CANONICAL).block_ids[0]
        publisher.sign(owner)
        # The contribution: derived knowledge only, citing evidence already held -- the change a
        # propose scope can stand in for. Touching canonical would (correctly) exceed it.
        task = publisher.define_task(source)
        report = publisher.validate(
            CandidateSet(
                producer=MODEL,
                candidates=[
                    Candidate(
                        memory_type=MemoryType.SEMANTIC,
                        evidence=[source],
                        payload={"kind": "concept", "label": "Laplace", "statement": "a transform"},
                    )
                ],
            ),
            task,
        )
        publisher.commit(report)
        publisher.sign(contributor)  # the contribution's head, signed only under propose
        await publisher.push(registry, "registry.example/contributor/brain", "proposal")

        consumer = Brain.open(tmp_path / "consumer", actor=SAM)
        with pytest.raises(InsufficientScopeError, match="explicitly not the published state"):
            await consumer.pull(registry, "registry.example/contributor/brain", "proposal")
        await consumer.pull(
            registry,
            "registry.example/contributor/brain",
            "proposal",
            verification=VerificationPolicy(allow_propose_head=True),
        )
