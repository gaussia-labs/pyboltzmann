"""Content a block names rather than carries, for any memory type.

A payload is JSON, hashed on every access, so a datum large enough to matter belongs in the store with
the block naming it. Canonical has always worked that way; nothing else did, and the three operations
that must account for those bytes -- packing a layer, marking before a sweep, destroying on redaction --
each asked ``isinstance(block, CanonicalBlock)`` to find them.

That made two of them silently wrong for any other schema: a sweep that deletes content a retained root
still names, and a redaction that leaves the bytes behind. The tests here pin the generic behaviour, so
the schema that eventually names content out of line inherits it rather than rediscovering it.

**Why these tests patch rather than declare a schema.** The obvious way to test this is a new episodic
schema that names content. It is also a trap: ``Block.__init_subclass__`` registers by
``(memory_type, schema_version)`` into a module-level registry, and ``build_block`` selects
``versions[-1]``. Declaring an episodic v99 anywhere in the suite silently makes it the schema every
episodic proposal is built as, and five tests in ``test_search`` fail on a payload they never wrote.
So the property is patched onto the real ``EpisodicBlock`` for the duration of a test, which is also the
more focused check: what these call sites owe is to consult ``content_digests``, whatever declares it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boltzmann.blocks.canonical import CanonicalBlock, NormalizedView
from boltzmann.blocks.content import ContentRef
from boltzmann.blocks.episodic import EpisodicBlock
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.distribution.layers import pack_module, required_blobs, unpack_layer
from boltzmann.exceptions import BlockTombstonedError
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.time import utc_timestamp
from boltzmann.indices.base import AbstractIndex, ContentReader, IndexKind
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.retention.policy import RetentionPolicy
from boltzmann.retention.reachability import reachable_from
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
DETECTOR = Producer(kind=ProducerKind.MODEL, id="detector", version="1")
TRANSCRIPT = b"the transcript of that episode, too large to inline in a payload" * 40


def _proposer(task: object, source: bytes) -> CandidateSet:
    return CandidateSet(
        producer=DETECTOR,
        candidates=[
            Candidate(
                memory_type=MemoryType.EPISODIC,
                evidence=[task.source],  # type: ignore[attr-defined]
                payload={"summary": "the lecture was recorded", "occurred_at": utc_timestamp()},
            )
        ],
    )


@pytest.fixture
def naming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A brain holding an episodic block whose content sits in the store.

    Yields the brain, the committed block id, and the reference the block names.
    """

    def build(**kwargs: object) -> tuple[Brain, object, ContentRef]:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, **kwargs)  # type: ignore[arg-type]
        reference = brain.put_content(TRANSCRIPT, media_type="text/plain")

        # The real EpisodicBlock, told to name that content. No registry entry, so no other test's
        # episodic proposals change shape.
        monkeypatch.setattr(
            EpisodicBlock,
            "content_digests",
            property(lambda self: (reference.blob,)),
            raising=False,
        )

        commit = brain.ingest(
            b"%PDF-1.7 lecture", RegistrationRequest(media_type="application/pdf", actor=CURATOR), _proposer
        )
        episodic = brain.module(MemoryType.EPISODIC).block_ids
        assert len(episodic) == 1, commit
        return brain, episodic[0], reference

    return build


@pytest.fixture
def dangling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A brain holding an episodic block that names content the store never received.

    The realistic paths into this state are an external proposer that puts a reference in a payload
    without materializing the bytes, and a brain copied without all of its blobs. Reached here by
    skipping ``put_content``, which is the same envelope either way.
    """

    def build(**kwargs: object) -> tuple[Brain, object, ContentRef]:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, **kwargs)  # type: ignore[arg-type]
        reference = ContentRef(blob=OciDigest.of(TRANSCRIPT), media_type="text/plain", size=len(TRANSCRIPT))
        assert not brain.store.has(reference.blob)

        monkeypatch.setattr(EpisodicBlock, "content_digests", property(lambda self: (reference.blob,)), raising=False)

        commit = brain.ingest(
            b"%PDF-1.7 lecture", RegistrationRequest(media_type="application/pdf", actor=CURATOR), _proposer
        )
        episodic = brain.module(MemoryType.EPISODIC).block_ids
        assert len(episodic) == 1, commit
        return brain, episodic[0], reference

    return build


class TestTheDefault:
    """A self-contained block names nothing, which is what keeps this change additive."""

    def test_a_block_without_content_names_nothing(self) -> None:
        assert EpisodicBlock(summary="s", occurred_at=utc_timestamp()).content_digests == ()

    def test_canonical_names_its_original(self) -> None:
        digest = OciDigest.of(b"%PDF-1.7")
        assert CanonicalBlock(blob=digest, media_type="application/pdf", size=8).content_digests == (digest,)

    def test_canonical_names_its_normalized_view_too(self) -> None:
        original, view = OciDigest.of(b"%PDF-1.7"), OciDigest.of(b"plain text")
        block = CanonicalBlock(
            blob=original,
            media_type="application/pdf",
            size=8,
            normalized_view=NormalizedView(blob=view, media_type="text/plain", size=10),
        )
        assert block.content_digests == (original, view)

    def test_a_normalized_view_is_a_content_reference(self) -> None:
        # The same three fields, so making it a ContentRef cannot have moved a canonical block_id. The
        # golden vectors are the other half of that proof.
        assert issubclass(NormalizedView, ContentRef)
        assert set(NormalizedView.model_fields) == set(ContentRef.model_fields)


class TestPutContent:
    """Materializing bytes a block will name is not registering evidence."""

    def test_it_returns_a_usable_reference(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        reference = brain.put_content(TRANSCRIPT, media_type="text/plain")

        assert reference.size == len(TRANSCRIPT)
        assert reference.media_type == "text/plain"
        assert brain.store.get_bytes(reference.blob) == TRANSCRIPT

    def test_it_commits_nothing(self, tmp_path: Path) -> None:
        # No block, no composition, no snapshot: nothing to commit until a block names it.
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        before = brain.snapshot()
        brain.put_content(TRANSCRIPT, media_type="text/plain")
        assert brain.snapshot() == before

    def test_identical_content_has_one_identity(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        first = brain.put_content(TRANSCRIPT, media_type="text/plain")
        second = brain.put_content(TRANSCRIPT, media_type="text/plain")
        assert first.blob == second.blob

    def test_content_nothing_names_is_reclaimed(self, tmp_path: Path) -> None:
        # The counterpart to the sweep tests: unreferenced content is exactly what a prune should collect.
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        orphan = brain.put_content(b"named by nothing", media_type="text/plain")

        report = brain.prune(dry_run=False)

        assert orphan.blob.hex in {digest.hex for digest in report.reclaimed}


class TestSweeping:
    """The failure that would have been data loss."""

    def test_reachability_reports_content_of_any_module(self, naming) -> None:
        brain, _, reference = naming()
        assert reference.blob.hex in reachable_from(brain.snapshot(), brain.store)

    def test_prune_keeps_content_a_retained_root_names(self, naming) -> None:
        brain, _, reference = naming()

        report = brain.prune(dry_run=False)

        assert reference.blob.hex not in {digest.hex for digest in report.reclaimed}
        assert brain.store.get_bytes(reference.blob) == TRANSCRIPT


class TestPacking:
    """The failure that would have published a pointer leading nowhere."""

    def test_a_layer_names_the_content(self, naming) -> None:
        brain, _, reference = naming()
        module = brain.module(MemoryType.EPISODIC)

        assert reference.blob.hex in {digest.hex for digest in required_blobs(module)}

    def test_the_content_survives_a_round_trip(self, naming) -> None:
        brain, block_id, reference = naming()
        layer = pack_module(brain.module(MemoryType.EPISODIC))

        elsewhere = MemoryBlockStore()
        composition = unpack_layer(layer, elsewhere)

        assert block_id in composition.block_ids
        assert elsewhere.get_bytes(reference.blob) == TRANSCRIPT


class TestIndicesReceiveAReader:
    """The case that started this: indexing what a block names, without being handed a store.

    Before, an index over canonical blocks got envelopes holding a digest, a media type and a size --
    nothing to index. The only way through was for the caller to construct the store itself, pass it to
    the index, and build the brain around both. Now the reader arrives with the blocks.
    """

    def test_an_index_can_read_the_content_a_block_names(self, tmp_path: Path) -> None:
        indexed: dict[str, bytes] = {}

        class ContentIndex(AbstractIndex):
            KIND = IndexKind.INVERTED

            def build(self, blocks, content) -> None:
                indexed.clear()
                for block in blocks:
                    for digest in block.content_digests:
                        indexed[digest.hex] = content.get_bytes(digest)

            def search(self, query, limit: int = 10):
                return []

        # Brain.open, one call, no store threaded through by hand.
        brain = Brain.open(tmp_path / "brain", actor=CURATOR, indices={MemoryType.CANONICAL: [ContentIndex()]})
        brain.register(b"%PDF-1.7 the lecture notes", RegistrationRequest(media_type="application/pdf", actor=CURATOR))

        assert list(indexed.values()) == [b"%PDF-1.7 the lecture notes"]

    def test_the_store_satisfies_the_reader_protocol(self, tmp_path: Path) -> None:
        # Structural, via get_bytes: the narrowing costs an implementation nothing, and there is no
        # second spelling of the same read to keep in sync.
        assert isinstance(Brain.open(tmp_path / "brain", actor=CURATOR).store, ContentReader)
        assert isinstance(MemoryBlockStore(), ContentReader)


class TestResolvability:
    """The state nothing reported: a whole block naming a datum that is gone.

    Every other reader is silent about it and correctly so. ``verify`` skips bytes it cannot read, so a
    block whose content vanished verifies. The composition verifies, because its root is over identities.
    A ``prune`` reclaims nothing, because a retained root still names the digest. The failure surfaced
    only at ``pack_module``, which is the last place it can be found and the worst: by then the snapshot
    was believed publishable.
    """

    def test_content_a_block_names_is_reported(self, naming) -> None:
        brain, _, reference = naming()

        report = brain.resolvability()

        assert reference.blob in report.content_resolvable[MemoryType.EPISODIC]
        assert report.is_intact

    def test_missing_content_is_reported_as_missing(self, dangling) -> None:
        brain, _, reference = dangling()

        report = brain.resolvability()

        assert reference.blob in report.content_missing[MemoryType.EPISODIC]
        assert not report.content_tombstoned

    def test_the_snapshot_is_not_intact_when_a_datum_is_gone(self, dangling) -> None:
        # The whole point: this is the call that now answers the question, before publication asks it.
        brain, _, _ = dangling()

        assert not brain.resolvability().is_intact

    def test_the_block_itself_is_still_reported_resolvable(self, dangling) -> None:
        # The block is whole. Saying otherwise would be a different lie, and would make a damaged
        # envelope indistinguishable from a missing datum.
        brain, block_id, _ = dangling()

        report = brain.resolvability()

        assert block_id in report.resolvable[MemoryType.EPISODIC]
        assert not report.missing

    def test_canonical_content_was_always_covered_by_this(self, tmp_path: Path) -> None:
        # Canonical has named its original since the beginning, so the gap was never episodic-only.
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        result = brain.register(b"%PDF-1.7 lecture", RegistrationRequest(media_type="application/pdf", actor=CURATOR))
        blob = brain.module(MemoryType.CANONICAL).get(result.block_id).content_digests[0]

        assert blob in brain.resolvability().content_resolvable[MemoryType.CANONICAL]

    def test_a_lawful_erasure_does_not_read_as_damage(self, naming) -> None:
        # Section 10.6 for content: redaction tombstones the block and its datum together, so neither
        # is missing. A store that reported this as corruption would be unusable after any erasure.
        brain, block_id, _ = naming(policy=RetentionPolicy(redactable_media_types=["text/plain"]))
        brain.redact(block_id, MemoryType.EPISODIC, reason="erasure request")

        report = brain.resolvability()

        assert block_id in report.tombstoned[MemoryType.EPISODIC]
        assert not report.content_missing
        assert report.is_intact

    def test_one_datum_named_twice_is_reported_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        reference = brain.put_content(TRANSCRIPT, media_type="text/plain")
        monkeypatch.setattr(EpisodicBlock, "content_digests", property(lambda self: (reference.blob,)), raising=False)

        def two_episodes(task: object, source: bytes) -> CandidateSet:
            return CandidateSet(
                producer=DETECTOR,
                candidates=[
                    Candidate(
                        memory_type=MemoryType.EPISODIC,
                        evidence=[task.source],  # type: ignore[attr-defined]
                        payload={"summary": summary, "occurred_at": utc_timestamp()},
                    )
                    for summary in ("the lecture was recorded", "the lecture was transcribed")
                ],
            )

        brain.ingest(
            b"%PDF-1.7 lecture", RegistrationRequest(media_type="application/pdf", actor=CURATOR), two_episodes
        )

        report = brain.resolvability()

        assert len(report.resolvable[MemoryType.EPISODIC]) == 2
        assert report.content_resolvable[MemoryType.EPISODIC] == [reference.blob]


class TestRedaction:
    """The failure that would have been a redaction that did not redact."""

    def test_it_destroys_the_content(self, naming) -> None:
        brain, block_id, reference = naming(policy=RetentionPolicy(redactable_media_types=["text/plain"]))

        result = brain.redact(block_id, MemoryType.EPISODIC, reason="erasure request")

        assert reference.blob.hex in {digest.hex for digest in result.redacted}
        with pytest.raises(BlockTombstonedError):
            brain.store.get_bytes(reference.blob)

    def test_the_content_is_tombstoned_not_missing(self, naming) -> None:
        # Section 10.6: destroyed must never be indistinguishable from corrupted.
        brain, block_id, reference = naming(policy=RetentionPolicy(redactable_media_types=["text/plain"]))
        brain.redact(block_id, MemoryType.EPISODIC, reason="erasure request")

        assert brain.store.has(reference.blob)
        assert not brain.store.is_resolvable(reference.blob)
