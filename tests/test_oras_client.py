"""The ORAS transport, against a fake registry.

**These tests do not prove the client works against a real registry.** They pin the wire shape -- OCI's
camelCase manifest, blob-existence checks before upload, digest verification on download -- and that the
adapter satisfies the transport interface. Whether ORAS and a given registry agree on authentication,
chunking, and redirects can only be established against a live one.

What they do catch is the class of bug that would otherwise surface only in production: a manifest this
SDK writes that no OCI tool can read, or a downloaded blob accepted without checking it hashes to what
was asked for.
"""

from __future__ import annotations

import json
import re
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
from boltzmann.exceptions import DistributionError, ReferenceNotFoundError
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
    """Enough of ``requests.Response`` that the client's status handling is exercised rather than bypassed."""

    def __init__(
        self,
        content: bytes = b"",
        status_code: int = 201,
        reason: str = "Created",
        content_type: str = "application/json",
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.reason = reason
        self.headers = {"content-type": content_type}

    @property
    def text(self) -> str:
        return self.content.decode(errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeContainer:
    """What ORAS hands back from ``get_container``: a parsed reference that knows its own URLs."""

    def __init__(self, target: str) -> None:
        self.target = target

    def manifest_url(self) -> str:
        return f"v2/{self.target}"

    def __str__(self) -> str:
        return self.target


class FakeAuthHeader:
    """What ``parse_auth_header`` returns: a challenge with a realm, a service and a scope."""

    def __init__(self, realm: str, service: str, scope: str | None = None) -> None:
        self.realm = realm
        self.service = service
        self.scope = scope


class FakeAuth:
    """A token backend that records the scopes it was asked for."""

    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry
        self.token: str | None = None

    def request_token(self, header: FakeAuthHeader) -> str:
        self.registry.write_scopes.append(header.scope or "")
        return "bearer-for-" + (header.scope or "nothing")

    def set_token_auth(self, token: str) -> None:
        self.token = token


class FakeRegistry:
    """Records what the client asked it to do, and serves back what it was given."""

    prefix = "https"

    challenge: str | None = 'Bearer realm="https://auth.example/token",service="registry.example"'
    """What the registry answers to an unauthenticated probe. ``None`` for a registry with no auth."""

    def __init__(self) -> None:
        self.auth = FakeAuth(self)
        self.session = FakeSession(self)
        self.write_scopes: list[str] = []
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, dict[str, Any]] = {}
        self.uploads: list[str] = []
        self.existence_checks: list[str] = []
        self.requests: list[str] = []
        self.serve: FakeResponse | None = None
        """Set to answer the next manifest request with something specific."""

    def get_container(self, target: str) -> FakeContainer:
        return FakeContainer(target)

    def do_request(self, url: str, method: str = "GET", **kwargs: Any) -> FakeResponse:
        """Serve a manifest the way a registry does: by status code.

        The client reads the status rather than catching an exception, because "no such tag" and "your
        credential was refused" have to be distinguishable. Modelling that here is what makes these tests
        exercise the distinction instead of assuming it.
        """
        self.requests.append(url)
        if self.serve is not None:
            return self.serve

        target = url.split("://", 1)[1].removeprefix("v2/")
        if target not in self.manifests:
            body = b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"manifest unknown"}]}'
            return FakeResponse(body, status_code=404, reason="Not Found")
        return FakeResponse(json.dumps(self.manifests[target]).encode(), status_code=200, reason="OK")

    def blob_exists(self, layer: dict[str, Any], container: FakeContainer) -> bool:
        self.existence_checks.append(layer["digest"])
        return layer["digest"] in self.blobs

    def upload_blob(self, blob: str, container: FakeContainer, layer: dict[str, Any], **kwargs: Any) -> FakeResponse:
        payload = Path(blob).read_bytes()
        self.blobs[layer["digest"]] = payload
        self.uploads.append(layer["digest"])
        return FakeResponse()

    def upload_manifest(self, manifest: dict[str, Any], container: FakeContainer) -> FakeResponse:
        self.manifests[str(container)] = manifest
        return FakeResponse()

    def get_blob(self, container: str, digest: str, **kwargs: Any) -> FakeResponse:
        if digest not in self.blobs:
            return FakeResponse(status_code=404)
        return FakeResponse(self.blobs[digest])


class FakeSession:
    """Serves the unauthenticated probe that carries the challenge."""

    def __init__(self, registry: FakeRegistry) -> None:
        self.registry = registry

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        headers = {}
        if self.registry.challenge is not None:
            headers["Www-Authenticate"] = self.registry.challenge
        response = FakeResponse(b"{}", status_code=401, reason="Unauthorized")
        response.headers.update(headers)
        return response


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
        with pytest.raises(ReferenceNotFoundError, match="not published"):
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


class TestTellingAbsenceFromFailure:
    """The distinction the fast-forward check depends on.

    ``push`` reads the remote before overwriting a tag, and treats "nothing is published here" as
    permission to proceed. If it cannot tell that apart from "the registry refused me", then an expired
    credential or a failing registry becomes a push over somebody else's version -- a safety check that
    fails open, which is worse than no check at all because it looks like one.
    """

    async def test_an_absent_tag_raises_the_narrow_error(self, client: OrasRegistryClient) -> None:
        with pytest.raises(ReferenceNotFoundError):
            await client.resolve(REFERENCE, "never-pushed")

    @pytest.mark.parametrize(("status", "reason"), [(401, "Unauthorized"), (403, "Forbidden"), (500, "Server Error")])
    async def test_a_refusal_is_not_reported_as_absence(
        self, client: OrasRegistryClient, fake: FakeRegistry, status: int, reason: str
    ) -> None:
        fake.serve = FakeResponse(b'{"errors":[{"message":"nope"}]}', status_code=status, reason=reason)

        with pytest.raises(DistributionError) as raised:
            await client.resolve(REFERENCE, "v1")
        assert not isinstance(raised.value, ReferenceNotFoundError)
        assert str(status) in str(raised.value)

    async def test_a_non_manifest_answer_says_where_to_look(
        self, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """Docker Hub's own trap: docker.io serves the marketing site, so a request there returns 200 and
        HTML. Reporting a JSON parse error would send the reader looking in the wrong place."""
        fake.serve = FakeResponse(b"<!DOCTYPE html><html>", status_code=200, reason="OK", content_type="text/html")

        with pytest.raises(DistributionError, match=re.escape("registry-1.docker.io")):
            await client.resolve(REFERENCE, "v1")

    async def test_a_transport_failure_is_not_absence(self, client: OrasRegistryClient, fake: FakeRegistry) -> None:
        def explode(*_: Any, **__: Any) -> FakeResponse:
            raise OSError("connection reset")

        fake.do_request = explode  # type: ignore[method-assign]

        with pytest.raises(DistributionError) as raised:
            await client.resolve(REFERENCE, "v1")
        assert not isinstance(raised.value, ReferenceNotFoundError)
        assert "cannot reach" in str(raised.value)

    async def test_a_push_refuses_rather_than_overwriting_when_the_remote_cannot_be_read(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """The bug this class exists for, end to end."""
        await brain.push(client, REFERENCE, "v1")
        fake.serve = FakeResponse(b'{"errors":[{"message":"token expired"}]}', status_code=401, reason="Unauthorized")

        brain.ingest(
            b"%PDF-1.7 Lecture 08",
            RegistrationRequest(media_type="application/pdf", actor=ALEX),
            proposing("Laplace"),
        )
        with pytest.raises(DistributionError, match="401"):
            await brain.push(client, REFERENCE, "v1")

    async def test_a_first_push_still_proceeds(self, brain: Brain, client: OrasRegistryClient) -> None:
        """Absence has to stay permission to proceed, or nothing could ever be published."""
        digest = await brain.push(client, REFERENCE, "fresh")
        assert digest is not None


class TestAuthorizingAWrite:
    """Asking for the scope a write needs, rather than the scope a challenge advertises.

    Docker Hub's upload endpoint answers ``Www-Authenticate`` with ``scope=repository:name:pull``, so a
    client that honours the challenge literally receives a read-only token and is then refused by the same
    registry -- whose error names ``pull`` and ``push`` as required. Nothing about the credentials was
    wrong. The scope for a write is therefore requested explicitly.
    """

    async def test_the_write_scope_is_requested_before_uploading(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        await brain.push(client, REFERENCE, "v1")
        assert fake.write_scopes, "no write scope was ever requested"
        for scope in fake.write_scopes:
            assert scope.endswith(":pull,push"), scope
            assert "org/brain" in scope

    async def test_a_registry_without_authentication_is_left_alone(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """No challenge means no token endpoint to ask, and a push that works must keep working."""
        fake.challenge = None
        await brain.push(client, REFERENCE, "v1")
        assert fake.write_scopes == []
        assert fake.uploads

    async def test_a_failing_token_endpoint_does_not_fail_the_push(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """It is an optimisation over the challenge-response path, so losing it must cost nothing."""

        def refuse(_: Any) -> str | None:
            raise OSError("token endpoint unreachable")

        fake.auth.request_token = refuse  # type: ignore[method-assign]
        await brain.push(client, REFERENCE, "v1")
        assert fake.uploads
