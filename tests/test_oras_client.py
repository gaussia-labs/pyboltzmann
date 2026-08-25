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

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.distribution.manifest import BrainManifest, Descriptor, build_signature_manifest
from boltzmann.distribution.media_types import (
    ARTIFACT_TYPE,
    HISTORY_MEDIA_TYPE,
    IMAGE_INDEX_MEDIA_TYPE,
    MANIFEST_MEDIA_TYPE,
    REF_NAME_ANNOTATION,
    SIGNATURE_MEDIA_TYPE,
)
from boltzmann.distribution.oras_client import OrasRegistryClient
from boltzmann.distribution.registry import RegistryClient
from boltzmann.exceptions import DistributionError, ReferenceNotFoundError
from boltzmann.identity.digest import OciDigest
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
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
        self.manifests: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.existence_checks: list[str] = []
        self.requests: list[str] = []
        self.serve: FakeResponse | None = None
        """Set to answer every request with something specific."""

        self.serve_put: FakeResponse | None = None
        """Set to answer only the manifest PUT, leaving reads working -- which is what a push needs, since
        it reads the remote before it writes."""

    def get_container(self, target: str) -> FakeContainer:
        return FakeContainer(target)

    def do_request(self, url: str, method: str = "GET", **kwargs: Any) -> FakeResponse:
        """Serve and store manifests the way a registry does: by status code, and by the bytes received.

        Storing what was *sent* rather than a re-serialization is the point. A digest is over bytes, so a
        fake that keeps a dictionary and hands back its own JSON could never catch a client whose published
        artifact is not the one it thinks it published.
        """
        self.requests.append(f"{method} {url}")
        if self.serve is not None:
            return self.serve

        target = url.split("://", 1)[1].removeprefix("v2/")
        if method == "PUT":
            if self.serve_put is not None:
                return self.serve_put
            payload = kwargs.get("data") or b""
            self.manifests[target] = payload
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            response = FakeResponse(b"", status_code=201, reason="Created")
            response.headers["Docker-Content-Digest"] = digest
            return response

        if target not in self.manifests:
            body = b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"manifest unknown"}]}'
            return FakeResponse(body, status_code=404, reason="Not Found")
        return FakeResponse(self.manifests[target], status_code=200, reason="OK")

    def blob_exists(self, layer: dict[str, Any], container: FakeContainer) -> bool:
        self.existence_checks.append(layer["digest"])
        return layer["digest"] in self.blobs

    def upload_blob(self, blob: str, container: FakeContainer, layer: dict[str, Any], **kwargs: Any) -> FakeResponse:
        payload = Path(blob).read_bytes()
        self.blobs[layer["digest"]] = payload
        self.uploads.append(layer["digest"])
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
    brain = Brain.open(tmp_path / "brain", actor=CURATOR)
    brain.ingest(
        b"%PDF-1.7 Lecture 07",
        RegistrationRequest(media_type="application/pdf", actor=CURATOR),
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
    """What this SDK publishes has to be a manifest an OCI tool can read.

    Every assertion here is against the bytes the registry received, not against a model. A manifest is a
    document with a digest, and the only thing that can be wrong about it is what was actually sent.
    """

    async def test_the_published_document_is_an_oci_manifest(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        await brain.push(client, REFERENCE, "v1")
        document = json.loads(fake.manifests[f"{REFERENCE}:v1"])

        assert document["schemaVersion"] == 2
        assert document["mediaType"] == MANIFEST_MEDIA_TYPE
        assert document["artifactType"] == ARTIFACT_TYPE
        assert set(document["config"]) >= {"mediaType", "digest", "size"}

    async def test_nothing_snake_case_survives_into_the_document(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """The failure this replaces: a layout whose index.json declared an OCI manifest and pointed at a
        document with ``artifact_type`` and no ``schemaVersion``, which no OCI tool can read."""
        await brain.push(client, REFERENCE, "v1")
        document = json.loads(fake.manifests[f"{REFERENCE}:v1"])

        def keys(value: object) -> list[str]:
            if isinstance(value, dict):
                return [*value, *(name for item in value.values() for name in keys(item))]
            if isinstance(value, list):
                return [name for item in value for name in keys(item)]
            return []

        assert [name for name in keys(document) if "_" in name and "." not in name] == []

    async def test_every_layer_is_a_descriptor(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        await brain.push(client, REFERENCE, "v1")
        for layer in json.loads(fake.manifests[f"{REFERENCE}:v1"])["layers"]:
            assert set(layer) >= {"mediaType", "digest", "size", "annotations"}
            assert layer["digest"].startswith("sha256:")
            # A module layer names the composition inside it; the history layer names no composition,
            # because what it carries is snapshot documents rather than blocks.
            if layer["mediaType"] == HISTORY_MEDIA_TYPE:
                assert "ai.gaussia.boltzmann.snapshot-count" in layer["annotations"]
            else:
                assert "ai.gaussia.boltzmann.merkle-root" in layer["annotations"]

    async def test_a_pushed_manifest_resolves_back(self, brain: Brain, client: OrasRegistryClient) -> None:
        """What was written on push must parse back into the same manifest on resolve."""
        await brain.push(client, REFERENCE, "v1")
        resolved = await client.resolve(REFERENCE, "v1")

        assert isinstance(resolved, BrainManifest)
        assert resolved.modules == brain.snapshot().installed
        for memory_type in brain.snapshot().installed:
            layer = resolved.layer_for(memory_type)
            assert layer is not None
            assert layer.merkle_root == brain.root_of(memory_type)

    async def test_the_tag_is_not_part_of_the_artifact(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """A tag is where an artifact sits, not part of what it is, so it must not change the digest.

        Writing it into the manifest gave the same brain a different name under every tag it was published
        as, and the digest the publisher computed matched neither.
        """
        first = await brain.push(client, REFERENCE, "v1")
        second = await brain.push(client, REFERENCE, "latest")
        assert first == second == brain.pack(tag="v1").digest

        for tag in ("v1", "latest"):
            document = json.loads(fake.manifests[f"{REFERENCE}:{tag}"])
            assert REF_NAME_ANNOTATION not in document.get("annotations", {})

    async def test_the_digest_is_the_one_the_registry_filed_it_under(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """Otherwise pinning by digest -- the only way to name a version somebody else can move a tag away
        from -- resolves to nothing."""
        returned = await brain.push(client, REFERENCE, "v1")
        stored = fake.manifests[f"{REFERENCE}:v1"]
        assert str(returned) == "sha256:" + hashlib.sha256(stored).hexdigest()

    async def test_a_registry_that_rewrites_the_manifest_is_reported(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """Two names for one artifact is not something to paper over."""
        response = FakeResponse(b"", status_code=201, reason="Created")
        response.headers["Docker-Content-Digest"] = "sha256:" + "00" * 32
        fake.serve_put = response

        with pytest.raises(DistributionError, match="two names"):
            await brain.push(client, REFERENCE, "v1")


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
        fake.serve_put = FakeResponse(b'{"errors":[]}', status_code=403, reason="Forbidden")
        with pytest.raises(DistributionError, match="failed with 403"):
            await brain.push(client, REFERENCE, "v1")

    async def test_uploads_work_from_a_store_without_files(
        self, client: OrasRegistryClient, fake: FakeRegistry
    ) -> None:
        """A store with no filesystem still has to be publishable; the blob is staged for the upload."""
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        brain.ingest(
            b"%PDF-1.7 Lecture 07",
            RegistrationRequest(media_type="application/pdf", actor=CURATOR),
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
        fake.manifests[f"{REFERENCE}:v1"] = json.dumps(
            {
                "schemaVersion": 2,
                "artifactType": "application/vnd.oci.image.manifest.v1+json",
                "config": {},
                "layers": [],
            }
        ).encode()
        with pytest.raises(DistributionError, match="not a Boltzmann brain"):
            await client.resolve(REFERENCE, "v1")


class TestFullCycleOverTheFakeTransport:
    """The same pull-modify-push cycle, driven through the ORAS adapter."""

    async def test_pull_modify_push(self, brain: Brain, client: OrasRegistryClient, tmp_path: Path) -> None:
        await brain.push(client, REFERENCE, "v1")

        target = Brain.open(tmp_path / "target", actor=CURATOR)
        await target.pull(client, REFERENCE, "v1")
        assert target.verify()
        assert target.root_of(MemoryType.SEMANTIC) == brain.root_of(MemoryType.SEMANTIC)

        source = target.module(MemoryType.CANONICAL).block_ids[0]
        task = target.define_task(source)
        target.commit(target.validate(proposing("Convolution")(task, b""), task))
        await target.push(client, tag="v2")

        again = Brain.open(tmp_path / "again", actor=CURATOR)
        await again.pull(client, REFERENCE, "v2")
        assert len(again.module(MemoryType.SEMANTIC)) == 2
        assert again.verify()

    async def test_what_the_registry_stored_is_what_the_layout_holds(
        self, brain: Brain, client: OrasRegistryClient, fake: FakeRegistry, tmp_path: Path
    ) -> None:
        """One artifact, one set of bytes, one name -- which is what makes publishing a copy rather than a
        conversion, as Section 7 requires of the on-disk format."""
        digest = await brain.push(client, REFERENCE, "v1")
        remote = fake.manifests[f"{REFERENCE}:v1"]

        local = brain.pack(tag="v1")
        assert local.to_bytes() == remote
        assert local.digest == digest


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
            RegistrationRequest(media_type="application/pdf", actor=CURATOR),
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


class TestReferrers:
    """The referrers listing is shared, unauthenticated space, and is read tolerantly.

    Other tools attach their own referrers (SBOMs, attestations) whose descriptors legally carry
    fields this SDK does not model. None of that may break discovering our own signatures.
    """

    SUBJECT = OciDigest.of(b"the referred brain manifest")

    def _signature_entry(self) -> dict[str, Any]:
        return {
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": SIGNATURE_MEDIA_TYPE,
            "digest": str(OciDigest.of(b"a signature manifest")),
            "size": 123,
        }

    def _listing_key(self) -> str:
        return f"registry.example/v2/org/brain/referrers/{self.SUBJECT}?artifactType={SIGNATURE_MEDIA_TYPE}"

    async def test_foreign_and_malformed_entries_never_hide_a_signature(
        self, fake: FakeRegistry, client: OrasRegistryClient
    ) -> None:
        listing = {
            "schemaVersion": 2,
            "mediaType": IMAGE_INDEX_MEDIA_TYPE,
            "manifests": [
                {
                    # A foreign attachment carrying perfectly legal OCI descriptor fields
                    # (platform, data) that this SDK's Descriptor model does not know.
                    "mediaType": MANIFEST_MEDIA_TYPE,
                    "artifactType": "application/vnd.example.sbom",
                    "digest": str(OciDigest.of(b"an sbom")),
                    "size": 9,
                    "platform": {"os": "linux", "architecture": "amd64"},
                    "data": "e30=",
                },
                {
                    # Claims our artifact type but cannot be parsed: skipped, never fatal.
                    "mediaType": MANIFEST_MEDIA_TYPE,
                    "artifactType": SIGNATURE_MEDIA_TYPE,
                    "digest": str(OciDigest.of(b"a broken entry")),
                    "size": 7,
                    "annotations": None,
                },
                "not even an object",
                self._signature_entry(),
            ],
        }
        fake.manifests[self._listing_key()] = json.dumps(listing).encode()
        found = await client.referrers(REFERENCE, self.SUBJECT, artifact_type=SIGNATURE_MEDIA_TYPE)
        assert [str(descriptor.digest) for descriptor in found] == [self._signature_entry()["digest"]]

    async def test_a_listing_without_a_manifests_array_is_empty_not_fatal(
        self, fake: FakeRegistry, client: OrasRegistryClient
    ) -> None:
        fake.manifests[self._listing_key()] = json.dumps(["not", "an", "index"]).encode()
        assert await client.referrers(REFERENCE, self.SUBJECT, artifact_type=SIGNATURE_MEDIA_TYPE) == []

    async def test_a_listing_that_is_not_json_is_a_distribution_error(
        self, fake: FakeRegistry, client: OrasRegistryClient
    ) -> None:
        fake.serve = FakeResponse(b"<html>proxy error page</html>", status_code=200, reason="OK")
        with pytest.raises(DistributionError, match="not JSON"):
            await client.referrers(REFERENCE, self.SUBJECT, artifact_type=SIGNATURE_MEDIA_TYPE)

    async def test_appending_to_the_fallback_preserves_foreign_entries(
        self, fake: FakeRegistry, client: OrasRegistryClient
    ) -> None:
        """The fallback tag is shared space too: a rewrite must not strip fields it does not model."""
        foreign = {
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": "application/vnd.example.sbom",
            "digest": str(OciDigest.of(b"an sbom")),
            "size": 9,
            "platform": {"os": "linux", "architecture": "amd64"},
            "urls": ["https://example.test/sbom"],
        }
        fallback_tag = f"sha256-{self.SUBJECT.hex}"
        fake.manifests[f"{REFERENCE}:{fallback_tag}"] = json.dumps(
            {"schemaVersion": 2, "mediaType": IMAGE_INDEX_MEDIA_TYPE, "manifests": [foreign]}
        ).encode()

        store = MemoryBlockStore()
        record_bytes = b'{"a":"record"}'
        record_digest = store.put_bytes(record_bytes)
        manifest = build_signature_manifest(
            record_digest,
            len(record_bytes),
            "SHA256:someprintedkey",
            OciDigest.of(b"a snapshot"),
            Descriptor(media_type=MANIFEST_MEDIA_TYPE, digest=self.SUBJECT, size=42, artifact_type=ARTIFACT_TYPE),
        )
        await client.push_referrer(REFERENCE, manifest, store)

        written = json.loads(fake.manifests[f"registry.example/v2/org/brain/manifests/{fallback_tag}"])
        assert foreign in written["manifests"], "the foreign entry must survive byte-for-byte"
        assert any(entry.get("artifactType") == SIGNATURE_MEDIA_TYPE for entry in written["manifests"]), (
            "the new signature entry must be appended beside it"
        )
