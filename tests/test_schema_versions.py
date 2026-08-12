"""Two live schema versions, and what each combination of SDK and brain has to do.

Semantic, episodic and procedural blocks gained a v2 that may name content it does not carry, so
for the first time the registry holds two versions of one memory type. That makes three questions
answerable only by test, because the interesting cases are about a *client* rather than about a
brain:

1. a current SDK reads a brain written by a current SDK
2. a current SDK reads a brain written before v2 existed
3. an older SDK meets a brain that uses v2 -- and must say so, early, and say what to do

(2) is the one that must not regress. A ``block_id`` commits to its ``schema_version`` because the
version is inside the hashed envelope, so a brain published under v1 must keep decoding to exactly
the same identities no matter how many versions are registered afterwards. The vectors in
``conformance/vectors/block_ids.json`` pin that for the identity math; what is pinned here is that
the *write* path does not drift either, since nothing in the golden vectors can see which version a
newly built block gets.

(3) is tested with ``old_client`` from ``conftest``, which forgets a registry entry. There is no
mock in it: the check under test reads the same registry, so forgetting v2 makes this process a
faithful stand-in for an SDK that never implemented it.

**Why the checks are on ``pull`` rather than on ``decode``.** ``decode`` refusing an unknown
version was already true and is not the guarantee that matters. The first decode during a pull is
conditional -- ``rebuild_indices`` only decodes for a module with a rebuildable index registered --
so a consumer with no indices installs a brain it cannot read *cleanly*, and discovers the problem
at the first query, arbitrarily later and with no reference to the artifact it came from. Refusing
before the first blob moves is the difference between a diagnosable failure and a confusing one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from boltzmann.blocks.base import Block
from boltzmann.blocks.content import ContentRef
from boltzmann.blocks.episodic import EpisodicBlock, EpisodicBlockV2
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.procedural import ProceduralBlock, ProceduralBlockV2
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.blocks.semantic import SemanticBlock, SemanticBlockV2
from boltzmann.brain import Brain
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.distribution.manifest import require_supported_schemas, schema_versions_of
from boltzmann.distribution.media_types import ANNOTATION_SCHEMA_VERSIONS
from boltzmann.exceptions import BlockSchemaError, DistributionError, ProtocolError
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.time import utc_timestamp
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.query.scan import searchable_text

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
SAM = Actor(id="sam", kind=ActorKind.AGENT)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REFERENCE = "registry.example/org/brain"

DIAGRAM = b"\x89PNG\r\n\x1a\n" + b"not really a png, but bytes all the same" * 40
"""The kind of datum that motivated v2: an interpretation whose subject is not text."""


def _payload(*, content: ContentRef | None = None) -> dict[str, Any]:
    """A semantic payload, optionally naming content."""
    payload: dict[str, Any] = {
        "kind": "concept",
        "label": "Phase diagram",
        "statement": "The diagram shows the solid-liquid transition",
    }
    if content is not None:
        payload["content"] = content.model_dump(mode="json")
    return payload


def _proposer(payloads: list[dict[str, Any]]) -> Callable[..., CandidateSet]:
    def propose(task: Any, source: bytes) -> CandidateSet:
        return CandidateSet(
            producer=MODEL,
            candidates=[
                Candidate(memory_type=MemoryType.SEMANTIC, evidence=[task.source], payload=payload)
                for payload in payloads
            ],
        )

    return propose


def _brain(path: Path, *, naming: int = 0, plain: int = 1, actor: Actor = CURATOR) -> Brain:
    """
    A brain holding ``plain`` semantic blocks and ``naming`` that name content.

    The three shapes this module needs -- v1-only, v2-only, and a module holding both -- are the
    same call with different counts, which is the point: mixing versions is not a special mode.
    """
    brain = Brain.open(path, actor=actor)
    payloads: list[dict[str, Any]] = []
    for index in range(plain):
        payload = _payload()
        payload["label"] = f"Plain {index}"
        payloads.append(payload)
    for index in range(naming):
        reference = brain.put_content(DIAGRAM + str(index).encode(), media_type="image/png")
        payload = _payload(content=reference)
        payload["label"] = f"Naming {index}"
        payloads.append(payload)
    brain.ingest(
        b"%PDF-1.7 lecture", RegistrationRequest(media_type="application/pdf", actor=actor), _proposer(payloads)
    )
    return brain


def _versions(brain: Brain) -> tuple[int, ...]:
    return brain.module(MemoryType.SEMANTIC).schema_versions()


class TestWhichVersionABlockIsWrittenAs:
    """The gap nothing covered: encoding was never asserted, only decoding.

    ``schema_version`` is part of the envelope, so choosing a version is choosing a ``block_id``.
    A default that quietly picked the newest would re-version every block written after any schema
    was registered, which is why these are the tests that hold the compatibility story up.
    """

    def test_a_payload_that_names_no_content_stays_v1(self) -> None:
        block = Block.build(MemoryType.SEMANTIC, _payload())
        assert type(block) is SemanticBlock
        assert block.SCHEMA_VERSION == 1

    def test_a_payload_that_names_content_becomes_v2(self) -> None:
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        block = Block.build(MemoryType.SEMANTIC, _payload(content=reference))
        assert type(block) is SemanticBlockV2
        assert block.content_digests == (reference.blob,)

    @pytest.mark.parametrize(
        ("memory_type", "payload", "v1", "v2"),
        [
            (MemoryType.SEMANTIC, _payload(), SemanticBlock, SemanticBlockV2),
            (
                MemoryType.EPISODIC,
                {"summary": "the lecture was recorded", "occurred_at": utc_timestamp()},
                EpisodicBlock,
                EpisodicBlockV2,
            ),
            (
                MemoryType.PROCEDURAL,
                {"label": "Integrate", "goal": "Obtain the coefficients", "steps": [{"action": "integrate"}]},
                ProceduralBlock,
                ProceduralBlockV2,
            ),
        ],
        ids=lambda value: getattr(value, "value", ""),
    )
    def test_every_type_that_gained_content_resolves_the_same_way(
        self, memory_type: MemoryType, payload: dict[str, Any], v1: type[Block], v2: type[Block]
    ) -> None:
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        assert type(Block.build(memory_type, payload)) is v1
        assert type(Block.build(memory_type, {**payload, "content": reference.model_dump(mode="json")})) is v2

    def test_provenance_gained_no_content_field(self) -> None:
        """Deliberate: the removal ledger must stay readable by every client, of every version."""
        assert Block.schemas(MemoryType.PROVENANCE) == (Block.registry()[(MemoryType.PROVENANCE, 1)],)

    def test_an_invalid_payload_reports_the_newest_schema_error(self) -> None:
        """Falling back through versions must not bury the diagnosis in the oldest one's vocabulary.

        A payload naming malformed content is invalid everywhere, but only v2 has a ``content``
        field to complain about. If the reported failure were v1's it would read "extra inputs are
        not permitted", pointing the author at the field they correctly used.
        """
        with pytest.raises(PydanticValidationError) as caught:
            Block.build(MemoryType.SEMANTIC, {**_payload(), "content": "not a reference"})
        detail = str(caught.value)
        assert SemanticBlockV2.__name__ in detail
        assert "extra_forbidden" not in detail

    def test_registering_a_third_version_does_not_move_what_a_plain_payload_resolves_to(self) -> None:
        """The policy in one test: adding a schema is additive, never retroactive.

        Declaring a class is normally a session-wide hazard, since ``__init_subclass__`` writes into
        a module global with no removal API. The autouse fixture in ``conftest`` is what makes it
        safe to do here, so this can assert on a real third version rather than on a stand-in.
        """

        class SemanticBlockV3(SemanticBlockV2):
            SCHEMA_VERSION = 3
            provenance_note: str | None = None

        assert len(Block.schemas(MemoryType.SEMANTIC)) == 3

        assert type(Block.build(MemoryType.SEMANTIC, _payload())) is SemanticBlock
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        assert type(Block.build(MemoryType.SEMANTIC, _payload(content=reference))) is SemanticBlockV2
        assert type(Block.build(MemoryType.SEMANTIC, {**_payload(), "provenance_note": "n"})) is SemanticBlockV3


class TestWhatAReferenceDeclares:
    """``media_type`` and ``size`` are hashed into ``block_id``, so a wrong one is permanent.

    They are also the two fields a consumer reads to decide whether to fetch content -- which it does
    before holding the bytes that would contradict them. Checked where they are written, since that
    is where the bytes are in hand.
    """

    def test_put_content_measures_the_size_rather_than_trusting_a_caller(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        reference = brain.put_content(DIAGRAM, media_type="image/png")
        assert reference.size == len(DIAGRAM)
        assert brain.store.get_bytes(reference.blob) == DIAGRAM

    @pytest.mark.parametrize(
        "media_type",
        ["png", " ", "", "image/", "/png", "image png", "image/png/extra", "\n", "image/png\r\nX-Evil: 1"],
        ids=["no-slash", "blank", "empty", "no-subtype", "no-type", "space", "two-slashes", "newline", "injection"],
    )
    def test_put_content_refuses_a_thing_that_is_not_a_media_type(self, tmp_path: Path, media_type: str) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        with pytest.raises(ProtocolError, match="not a usable media type"):
            brain.put_content(DIAGRAM, media_type=media_type)

    @pytest.mark.parametrize(
        "media_type",
        ["IMAGE/PNG", "image/PNG", "image/png; charset=utf-8", "image/png;", "image/png ", "a" * 200 + "/b"],
        ids=["upper", "upper-subtype", "parameter", "trailing-semicolon", "trailing-space", "too-long"],
    )
    def test_a_spelling_a_parser_forgives_and_a_digest_does_not(self, tmp_path: Path, media_type: str) -> None:
        """Each of these parses cleanly. Each would give the same content two identities.

        ``media_type`` is hashed into ``block_id``, so a value merely *equivalent* to another under
        the RFC's own comparison rules is not equivalent here. Refused rather than normalized, for
        the reason ``LocalLayoutRegistry`` refuses a reference rather than rewriting it: filing a
        caller's content under a string they never passed is worse than telling them it is unusable.
        """
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        with pytest.raises(ProtocolError, match=r"not a usable media type|bounds a type"):
            brain.put_content(DIAGRAM, media_type=media_type)

    @pytest.mark.parametrize(
        "media_type",
        [
            "image/png",
            "application/octet-stream",
            "text/plain",
            "application/vnd.ms-excel",
            "audio/ogg",
            "application/vnd.oci.image.manifest.v1+json",
            "image/x-custom",
            "application/ld+json",
        ],
    )
    def test_real_media_types_are_accepted(self, tmp_path: Path, media_type: str) -> None:
        """Grammar, not a registry lookup: IANA moves without this SDK moving.

        Vendor trees, ``x-`` subtypes and structured ``+json`` suffixes all have to pass, or the
        check would be refusing types that are correct today and types that become correct later.
        """
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        assert brain.put_content(DIAGRAM, media_type=media_type).media_type == media_type

    def test_the_model_itself_still_accepts_anything(self) -> None:
        """Deliberate. A validator on ``ContentRef`` would run on ``decode`` too.

        ``NormalizedView`` extends ``ContentRef`` and takes its media type from a third-party
        normalization pipeline, so a malformed one may already sit inside a published canonical
        ``block_id``. Refusing it at decode would make this SDK unable to read a brain an older one
        wrote -- being stricter about the past rather than about the future.
        """
        ContentRef(blob=OciDigest.of(DIAGRAM), media_type="not a media type", size=0)

    def test_the_gate_refuses_a_payload_the_sdk_would_never_have_built(self, tmp_path: Path) -> None:
        """A proposer composes payloads itself, so the write path is not the only way in."""
        brain = _brain(tmp_path / "brain", plain=1)
        source = brain.module(MemoryType.CANONICAL).block_ids[0]
        task = brain.define_task(source)

        lying = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="notamediatype", size=len(DIAGRAM))
        report = brain.validate(_proposer([_payload(content=lying)])(task, b""), task)
        assert not report.is_clean
        assert any(issue.code == "content-mismatch" for result in report.results for issue in result.issues)

    def test_the_gate_catches_a_size_that_contradicts_the_stored_bytes(self, tmp_path: Path) -> None:
        brain = _brain(tmp_path / "brain", plain=1)
        stored = brain.put_content(DIAGRAM, media_type="image/png")
        source = brain.module(MemoryType.CANONICAL).block_ids[0]
        task = brain.define_task(source)

        lying = stored.model_copy(update={"size": stored.size + 4096})
        report = brain.validate(_proposer([_payload(content=lying)])(task, b""), task)
        assert not report.is_clean
        detail = next(issue.detail for result in report.results for issue in result.issues)
        assert "declares" in detail
        assert "the store holds" in detail

    def test_content_the_store_does_not_hold_is_not_an_error(self, tmp_path: Path) -> None:
        """A block may legitimately name bytes this brain never received; a selective install does that."""
        brain = _brain(tmp_path / "brain", plain=1)
        source = brain.module(MemoryType.CANONICAL).block_ids[0]
        task = brain.define_task(source)

        absent = ContentRef(blob=OciDigest.of(b"never stored"), media_type="image/png", size=12)
        report = brain.validate(_proposer([_payload(content=absent)])(task, b""), task)
        assert report.is_clean, [issue.detail for r in report.results for issue in r.issues]


class TestV2IsStillKnowledge:
    """A block whose datum is binary must not fall out of the parts of the protocol that read text."""

    def test_the_statement_is_still_required(self) -> None:
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        with pytest.raises(Exception, match="statement"):
            SemanticBlockV2.model_validate(
                {"kind": "concept", "label": "L", "content": reference.model_dump(mode="json")}
            )

    def test_it_remains_searchable_by_text(self) -> None:
        """A block invisible to every query would be knowledge the brain cannot retrieve."""
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        block = SemanticBlockV2.model_validate(_payload(content=reference))
        assert "The diagram shows the solid-liquid transition" in searchable_text(block)

    def test_it_is_still_a_semantic_block_to_every_reader(self) -> None:
        """``scan`` and the query path dispatch on the v1 type, so v2 has to satisfy it."""
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        assert isinstance(SemanticBlockV2.model_validate(_payload(content=reference)), SemanticBlock)


class TestTheThreeBrains:
    """v1-only, v2-only, and one module holding both."""

    def test_a_brain_that_uses_no_content_declares_only_v1(self, tmp_path: Path) -> None:
        assert _versions(_brain(tmp_path / "brain", plain=2)) == (1,)

    def test_a_brain_that_only_names_content_declares_only_v2(self, tmp_path: Path) -> None:
        assert _versions(_brain(tmp_path / "brain", plain=0, naming=2)) == (2,)

    def test_a_module_may_hold_both_versions(self, tmp_path: Path) -> None:
        brain = _brain(tmp_path / "brain", plain=2, naming=2)
        assert _versions(brain) == (1, 2)
        assert len(brain.module(MemoryType.SEMANTIC)) == 4

    def test_a_mixed_module_verifies_and_proves(self, tmp_path: Path) -> None:
        """The root is a function of block ids, so a version boundary is not a composition boundary."""
        brain = _brain(tmp_path / "brain", plain=2, naming=2)
        module = brain.module(MemoryType.SEMANTIC)
        assert brain.verify()
        for block_id in module.block_ids:
            assert module.inclusion_proof(block_id).verify(module.root)

    def test_content_a_mixed_module_names_travels_with_it(self, tmp_path: Path) -> None:
        from boltzmann.distribution.layers import required_blobs

        brain = _brain(tmp_path / "brain", plain=1, naming=1)
        module = brain.module(MemoryType.SEMANTIC)
        named = [digest for block in module.blocks() for digest in block.content_digests]
        assert named, "the v2 block should name its content"
        assert all(digest in required_blobs(module) for digest in named)


class TestACurrentSdkReadsAnything:
    """Requirement 1 and 2: a current client reads brains of either shape, round trip included."""

    @pytest.mark.parametrize(("plain", "naming"), [(2, 0), (0, 2), (2, 2)], ids=["v1-only", "v2-only", "mixed"])
    async def test_pack_pull_and_verify(self, tmp_path: Path, plain: int, naming: int) -> None:
        registry = LocalLayoutRegistry(tmp_path / "registry")
        source = _brain(tmp_path / "a", plain=plain, naming=naming)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")

        assert target.verify()
        for memory_type in source.snapshot().installed:
            assert target.root_of(memory_type) == source.root_of(memory_type)

    def test_a_v1_brain_keeps_its_identities_once_v2_exists(self, tmp_path: Path) -> None:
        """The regression that would matter most, stated over what a published brain committed to.

        Both versions are registered throughout this test -- there is no mode where they are not --
        so the block ids below are what a brain written before v2 existed already holds. They have
        to be reproduced exactly, not merely decoded without error.
        """
        brain = _brain(tmp_path / "brain", plain=3)
        module = brain.module(MemoryType.SEMANTIC)
        for block_id in module.block_ids:
            block = module.get(block_id)
            assert block.SCHEMA_VERSION == 1
            assert block.block_id == block_id
            assert Block.decode(block.canonical_bytes()).block_id == block_id

    def test_appending_to_a_v1_brain_keeps_writing_v1(self, tmp_path: Path) -> None:
        """Otherwise merely upgrading the SDK would make an existing brain unreadable to its peers."""
        brain = _brain(tmp_path / "brain", plain=1)
        assert _versions(brain) == (1,)

        source = brain.module(MemoryType.CANONICAL).block_ids[0]
        task = brain.define_task(source)
        payload = _payload()
        payload["label"] = "Added later"
        report = brain.validate(_proposer([payload])(task, b""), task)
        brain.commit(report)
        assert _versions(brain) == (1,)


class TestAnOlderSdkMeetsANewerBrain:
    """Requirement 3: refuse early, and say what to do about it."""

    async def test_it_still_reads_a_v1_brain(
        self, tmp_path: Path, old_client: Callable[[MemoryType, int], None]
    ) -> None:
        """The control. Forgetting v2 must cost nothing for a brain that never used it."""
        registry = LocalLayoutRegistry(tmp_path / "registry")
        await _brain(tmp_path / "a", plain=2).push(registry, REFERENCE, "v1")

        old_client(MemoryType.SEMANTIC, 2)
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1")
        assert target.verify()

    async def test_a_v2_brain_is_refused_with_the_remedy(
        self, tmp_path: Path, old_client: Callable[[MemoryType, int], None]
    ) -> None:
        registry = LocalLayoutRegistry(tmp_path / "registry")
        await _brain(tmp_path / "a", plain=0, naming=2).push(registry, REFERENCE, "v1")

        old_client(MemoryType.SEMANTIC, 2)
        target = Brain.open(tmp_path / "b", actor=SAM)
        with pytest.raises(DistributionError, match="schema version 2") as caught:
            await target.pull(registry, REFERENCE, "v1")

        detail = str(caught.value)
        assert "upgrade boltzmann" in detail, "the error has to name the remedy, not only the cause"
        assert "this client implements [1]" in detail

    async def test_nothing_is_downloaded_before_the_refusal(
        self, tmp_path: Path, old_client: Callable[[MemoryType, int], None]
    ) -> None:
        """The whole point of declaring versions on the manifest: pay nothing to find out."""
        registry = LocalLayoutRegistry(tmp_path / "registry")
        await _brain(tmp_path / "a", plain=0, naming=2).push(registry, REFERENCE, "v1")

        old_client(MemoryType.SEMANTIC, 2)
        target = Brain.open(tmp_path / "b", actor=SAM)
        pulled: list[str] = []
        original = registry.pull_blob

        async def counting(reference: str, digest: OciDigest, store: Any) -> None:
            pulled.append(digest.hex)
            await original(reference, digest, store)

        registry.pull_blob = counting  # type: ignore[method-assign]
        with pytest.raises(DistributionError, match="schema version 2"):
            await target.pull(registry, REFERENCE, "v1")
        assert pulled == [], f"refused only after downloading {len(pulled)} blobs"

    async def test_a_readable_module_of_a_mixed_brain_still_installs(
        self, tmp_path: Path, old_client: Callable[[MemoryType, int], None]
    ) -> None:
        """Scoped to what was asked for. Refusing the artifact would deny knowledge this client can read."""
        registry = LocalLayoutRegistry(tmp_path / "registry")
        source = _brain(tmp_path / "a", plain=1, naming=1)
        await source.push(registry, REFERENCE, "v1")

        old_client(MemoryType.SEMANTIC, 2)
        target = Brain.open(tmp_path / "b", actor=SAM)
        await target.pull(registry, REFERENCE, "v1", modules=[MemoryType.CANONICAL])

        assert target.snapshot().installed == [MemoryType.CANONICAL]
        assert target.root_of(MemoryType.CANONICAL) == source.root_of(MemoryType.CANONICAL)

    def test_decoding_a_v2_block_directly_names_the_remedy_too(
        self, tmp_path: Path, old_client: Callable[[MemoryType, int], None]
    ) -> None:
        """A local layout carries no manifest check, so the decode message is the last line of defence."""
        reference = ContentRef(blob=OciDigest.of(DIAGRAM), media_type="image/png", size=len(DIAGRAM))
        stored = SemanticBlockV2.model_validate(_payload(content=reference)).canonical_bytes()

        old_client(MemoryType.SEMANTIC, 2)
        with pytest.raises(BlockSchemaError, match="upgrade boltzmann") as caught:
            Block.decode(stored)
        assert "this client knows [1]" in str(caught.value)


class TestTheManifestDeclaration:
    """The annotation itself: what it says, and what its absence must not be read as."""

    def test_pack_declares_the_versions_each_module_holds(self, tmp_path: Path) -> None:
        manifest = _brain(tmp_path / "brain", plain=1, naming=1).pack(tag="v1")
        declared = schema_versions_of(manifest)
        assert declared[MemoryType.SEMANTIC] == (1, 2)
        assert declared[MemoryType.CANONICAL] == (1,)

    def test_the_declaration_is_deterministic(self, tmp_path: Path) -> None:
        """It travels inside the manifest, whose digest is what push dedup and fast-forward compare."""
        brain = _brain(tmp_path / "brain", plain=1, naming=1)
        assert brain.pack(tag="v1").to_bytes() == brain.pack(tag="v1").to_bytes()

    def test_a_caller_cannot_overwrite_the_protocol_version(self, tmp_path: Path) -> None:
        """It was splatted last, so the one annotation a consumer refuses on could be disabled."""
        from boltzmann.distribution.manifest import build_manifest
        from boltzmann.distribution.media_types import ANNOTATION_PROTOCOL_VERSION

        brain = _brain(tmp_path / "brain", plain=1)
        packed = brain.pack(tag="v1")
        manifest = build_manifest(
            brain.snapshot(),
            packed.config,
            packed.layers,
            annotations={ANNOTATION_PROTOCOL_VERSION: "99"},
        )
        assert manifest.annotations[ANNOTATION_PROTOCOL_VERSION] == "1"

    def test_an_undeclared_artifact_is_not_read_as_permission(self, tmp_path: Path) -> None:
        """An older publisher said nothing. Silence is unknown, and must fall through, not pass."""
        brain = _brain(tmp_path / "brain", plain=1, naming=1)
        packed = brain.pack(tag="v1")
        without = packed.model_copy(
            update={"annotations": {k: v for k, v in packed.annotations.items() if k != ANNOTATION_SCHEMA_VERSIONS}}
        )
        assert schema_versions_of(without) == {}
        require_supported_schemas(without, [MemoryType.SEMANTIC])

    @pytest.mark.parametrize(
        "value",
        ["not json", "[]", '{"semantic": "two"}', '{"semantic": [true]}', '{"mythical": [1]}'],
        ids=["malformed", "not-an-object", "not-a-list", "bool-is-not-a-version", "unknown-memory-type"],
    )
    def test_a_hostile_declaration_does_not_crash_the_consumer(self, tmp_path: Path, value: str) -> None:
        """Registry-supplied, so the types are untrusted as much as the values."""
        packed = _brain(tmp_path / "brain", plain=1).pack(tag="v1")
        hostile = packed.model_copy(update={"annotations": {**packed.annotations, ANNOTATION_SCHEMA_VERSIONS: value}})
        assert MemoryType.SEMANTIC not in schema_versions_of(hostile)
        require_supported_schemas(hostile, [MemoryType.SEMANTIC])
