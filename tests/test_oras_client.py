"""The ORAS transport, against a fake registry.

**These tests do not prove the client works against a real registry.** They pin the wire shape -- OCI's
camelCase manifest, blob-existence checks before upload, digest verification on download -- and that the
adapter satisfies the transport interface. Whether ORAS and a given registry agree on authentication,
chunking, and redirects can only be established against a live one.

What they do catch is the class of bug that would otherwise surface only in production: a manifest this
SDK writes that no OCI tool can read, or a downloaded blob accepted without checking it hashes to what
was asked for.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.distribution.manifest import BrainManifest
from boltzmann.distribution.media_types import ARTIFACT_TYPE, MANIFEST_MEDIA_TYPE, REF_NAME_ANNOTATION
from boltzmann.distribution.oras_client import OrasRegistryClient
from boltzmann.distribution.registry import RegistryClient
from boltzmann.exceptions import DistributionError
from boltzmann.identity.digest import OciDigest
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.store.memory import MemoryBlockStore

ALEX = Actor(id="alex", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REFERENCE = "registry.example/org/brain"


def llm(task, source: bytes) -> CandidateSet:
    return proposing("Fourier")(task, source)


def proposing(label: str):
    """A stub proposer. Distinct labels, because an identical proposal is a duplicate and is rejected."""

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


class FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 201) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRegistry:
    """Records what the client asked it to do, and serves back what it was given."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, dict[str, Any]] = {}
        self.uploads: list[str] = []
        self.existence_checks: list[str] = []

    def get_container(self, target: str) -> str:
        return target

    def blob_exists(self, layer: dict[str, Any], container: str) -> bool:
        self.existence_checks.append(layer["digest"])
        return layer["digest"] in self.blobs

    def upload_blob(self, blob: str, container: str, layer: dict[str, Any], **kwargs: Any) -> FakeResponse:
        payload = Path(blob).read_bytes()
        self.blobs[layer["digest"]] = payload
        self.uploads.append(layer["digest"])
        return FakeResponse()

    def upload_manifest(self, manifest: dict[str, Any], container: str) -> FakeResponse:
        self.manifests[container] = manifest
        return FakeResponse()

    def get_manifest(self, target: str, **kwargs: Any) -> dict[str, Any]:
        if target not in self.manifests:
            raise KeyError(target)
        return self.manifests[target]

    def get_blob(self, container: str, digest: str, **kwargs: Any) -> FakeResponse:
        if digest not in self.blobs:
            return FakeResponse(status_code=404)
        return FakeResponse(self.blobs[digest])


@pytest.fixture
def fake() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def client(fake: FakeRegistry) -> OrasRegistryClient:
    return OrasRegistryClient(registry=fake)


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    brain = Brain.open(tmp_path / "brain", actor=ALEX)
    brain.ingest(
        b"%PDF-1.7 Lecture 07",
        RegistrationRequest(media_type="application/pdf", actor=ALEX),
        llm,
    )
    return brain


class TestInterface:
    def test_satisfies_the_transport_interface(self, client: OrasRegistryClient) -> None:
        assert isinstance(client, RegistryClient)

    def test_oras_is_imported_lazily(self) -> None:
        """The core installs without the extra, so importing must not require it."""
        fresh = OrasRegistryClient()
        assert fresh._registry is None


class TestWireShape:
    """What this SDK writes has to be a manifest an OCI tool can read."""

    async def test_the_manifest_is_oci_camel_case(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        await brain.push(client, REFERENCE, "v1")
        document = fake.manifests[f"{REFERENCE}:v1"]

        assert document["schemaVersion"] == 2
        assert document["mediaType"] == MANIFEST_MEDIA_TYPE
        assert document["artifactType"] == ARTIFACT_TYPE
        assert set(document["config"]) >= {"mediaType", "digest", "size"}
        assert document["annotations"][REF_NAME_ANNOTATION] == "v1"

    async def test_every_layer_is_a_descriptor(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        await brain.push(client, REFERENCE, "v1")
        for layer in fake.manifests[f"{REFERENCE}:v1"]["layers"]:
            assert set(layer) >= {"mediaType", "digest", "size", "annotations"}
            assert layer["digest"].startswith("sha256:")
            assert "ai.gaussia.boltzmann.merkle-root" in layer["annotations"]

    async def test_a_pushed_manifest_resolves_back(self, brain: Brain, client: OrasRegistryClient) -> None:
        """The camelCase written on push must parse back into the same manifest on resolve."""
        await brain.push(client, REFERENCE, "v1")
        resolved = await client.resolve(REFERENCE, "v1")

        assert isinstance(resolved, BrainManifest)
        assert resolved.modules == brain.snapshot().installed
        for memory_type in brain.snapshot().installed:
            layer = resolved.layer_for(memory_type)
            assert layer is not None
            assert layer.merkle_root == brain.root_of(memory_type)

    async def test_the_tag_annotation_is_not_kept_as_manifest_content(
        self, brain: Brain, client: OrasRegistryClient
    ) -> None:
        """A tag is where an artifact sits, not part of what it is, so it must not change the digest."""
        await brain.push(client, REFERENCE, "v1")
        resolved = await client.resolve(REFERENCE, "v1")
        assert REF_NAME_ANNOTATION not in resolved.annotations
        assert resolved.digest == brain.pack(tag="v1").digest


class TestUploads:
    async def test_uploads_config_and_every_layer(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        await brain.push(client, REFERENCE, "v1")
        manifest = brain.pack(tag="v1")
        expected = {str(manifest.config.digest)} | {str(layer.digest) for layer in manifest.layers}
        assert expected <= set(fake.uploads)

    async def test_skips_blobs_the_registry_already_holds(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """This is what makes an update transfer one layer instead of the whole brain."""
        await brain.push(client, REFERENCE, "v1")
        first = len(fake.uploads)

        await brain.push(client, REFERENCE, "v2")
        assert len(fake.uploads) == first
        assert len(fake.existence_checks) > first

    async def test_a_failed_upload_is_reported(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        fake.upload_blob = lambda *a, **k: FakeResponse(status_code=500)  # type: ignore[method-assign]
        with pytest.raises(DistributionError, match="failed with 500"):
            await brain.push(client, REFERENCE, "v1")

    async def test_a_failed_manifest_publish_is_reported(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        fake.upload_manifest = lambda *a, **k: FakeResponse(status_code=403)  # type: ignore[method-assign]
        with pytest.raises(DistributionError, match="failed with 403"):
            await brain.push(client, REFERENCE, "v1")

    async def test_uploads_work_from_a_store_without_files(
        self, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """A store with no filesystem still has to be publishable; the blob is staged for the upload."""
        brain = Brain(MemoryBlockStore(), actor=ALEX)
        brain.ingest(
            b"%PDF-1.7 Lecture 07",
            RegistrationRequest(media_type="application/pdf", actor=ALEX),
            llm,
        )
        await brain.push(client, REFERENCE, "v1")
        assert fake.uploads


class TestDownloads:
    async def test_verifies_what_it_downloaded(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """A registry that serves the wrong bytes must not have them land under the asked-for digest."""
        await brain.push(client, REFERENCE, "v1")
        digest = OciDigest.of(b"%PDF-1.7 Lecture 07")
        fake.blobs[str(digest)] = b"tampered"

        with pytest.raises(DistributionError, match="hash to"):
            await client.pull_blob(REFERENCE, digest, MemoryBlockStore())

    async def test_a_missing_blob_is_reported(self, client: OrasRegistryClient) -> None:
        with pytest.raises(DistributionError, match="cannot fetch"):
            await client.pull_blob(REFERENCE, OciDigest.of(b"absent"), MemoryBlockStore())

    async def test_an_unresolvable_tag_is_reported(self, client: OrasRegistryClient) -> None:
        with pytest.raises(DistributionError, match="cannot resolve"):
            await client.resolve(REFERENCE, "nope")

    async def test_a_foreign_artifact_is_refused(self, client: OrasRegistryClient, fake: FakeRegistry) -> None:
        fake.manifests[f"{REFERENCE}:v1"] = {
            "schemaVersion": 2,
            "artifactType": "application/vnd.oci.image.manifest.v1+json",
            "config": {},
            "layers": [],
        }
        with pytest.raises(DistributionError, match="not a Boltzmann brain"):
            await client.resolve(REFERENCE, "v1")


class TestFullCycleOverTheFakeTransport:
    """The same pull-modify-push cycle, driven through the ORAS adapter."""

    async def test_pull_modify_push(self, brain: Brain, client: OrasRegistryClient, tmp_path: Path) -> None:
        await brain.push(client, REFERENCE, "v1")

        target = Brain.open(tmp_path / "target", actor=ALEX)
        await target.pull(client, REFERENCE, "v1")
        assert target.verify()
        assert target.root_of(MemoryType.SEMANTIC) == brain.root_of(MemoryType.SEMANTIC)

        source = target.module(MemoryType.CANONICAL).block_ids[0]
        task = target.define_task(source)
        target.commit(target.validate(proposing("Convolution")(task, b""), task))
        await target.push(client, tag="v2")

        again = Brain.open(tmp_path / "again", actor=ALEX)
        await again.pull(client, REFERENCE, "v2")
        assert len(again.module(MemoryType.SEMANTIC)) == 2
        assert again.verify()

    async def test_the_manifest_is_json_serializable(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """ORAS puts the manifest on the wire as JSON, so nothing in it may resist encoding."""
        await brain.push(client, REFERENCE, "v1")
        json.dumps(fake.manifests[f"{REFERENCE}:v1"])
