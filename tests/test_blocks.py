"""The block envelope, and what it does and does not bind into an identity."""

import json

import pytest
from pydantic import ValidationError

from boltzmann.blocks.base import ENVELOPE_KEYS, Block
from boltzmann.blocks.canonical import CanonicalBlock, NormalizedView
from boltzmann.blocks.episodic import EpisodicBlock
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.procedural import ProceduralBlock, Step
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    DerivationRecord,
    Producer,
    ProducerKind,
    ProvenanceBlock,
    RegistrationRecord,
    RemovalMechanism,
    RemovalRecord,
)
from boltzmann.blocks.semantic import Relation, SemanticBlock, SemanticKind
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import BlockIntegrityError, BlockSchemaError, NonDeterministicValueError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.identity.serialization import canonicalize

PDF = b"%PDF-1.7 lecture notes"
ACTOR = Actor(id="curator", kind=ActorKind.HUMAN)


def semantic(**overrides: object) -> SemanticBlock:
    fields: dict = {
        "kind": SemanticKind.FORMULA,
        "label": "Fourier series",
        "statement": "f(x) = a0/2 + ...",
    }
    fields.update(overrides)
    return SemanticBlock(**fields)


class TestEnvelope:
    """What gets hashed, and what stays outside."""

    def test_envelope_has_exactly_the_protocol_keys(self) -> None:
        assert set(semantic().envelope()) == ENVELOPE_KEYS

    def test_envelope_binds_type_and_schema_version(self) -> None:
        envelope = semantic().envelope()
        assert envelope["memory_type"] == "semantic"
        assert envelope["schema_version"] == 1
        assert envelope["boltzmann"] == PROTOCOL_VERSION
        assert envelope["serialization"] == "jcs/1"

    def test_canonical_bytes_are_a_fixed_point_of_canonicalization(self) -> None:
        """Re-canonicalizing the parsed envelope must reproduce the same bytes."""
        data = semantic().canonical_bytes()
        assert canonicalize(json.loads(data)) == data

    def test_canonical_bytes_carry_no_insignificant_whitespace(self) -> None:
        data = semantic().canonical_bytes()
        assert data.startswith(b'{"boltzmann":1,')
        assert b": " not in data
        assert b", " not in data
        assert b"\n" not in data

    def test_block_id_is_the_hash_of_the_canonical_bytes(self) -> None:
        block = semantic()
        assert block.block_id == BlockId.of(block.canonical_bytes())

    def test_identity_is_stable_across_construction(self) -> None:
        assert semantic().block_id == semantic().block_id

    def test_memory_type_is_part_of_the_identity(self) -> None:
        """Two blocks with the same payload but different types are different blocks."""
        procedural = ProceduralBlock(label="x", goal="y", steps=[Step(action="z")])
        assert json.loads(procedural.canonical_bytes())["memory_type"] == "procedural"

    def test_absent_optional_is_not_serialized_as_null(self) -> None:
        """``{"a": 1}`` and ``{"a": 1, "b": null}`` must be the same block."""
        assert semantic().block_id == semantic(subject=None).block_id
        assert b"null" not in semantic().canonical_bytes()

    def test_present_optional_changes_the_identity(self) -> None:
        assert semantic().block_id != semantic(subject="signals").block_id


class TestPayloadDomain:
    """Values with no portable canonical form cannot enter a block."""

    def test_float_is_refused_at_construction(self) -> None:
        with pytest.raises(ValidationError) as caught:
            SemanticBlock(kind=SemanticKind.FACT, label="l", statement="s", aliases=[1.5])  # type: ignore[list-item]
        assert caught.value.errors()

    def test_float_in_an_extra_field_is_refused(self) -> None:
        """``extra="forbid"`` means an unexpected field never reaches the hash at all."""
        with pytest.raises(ValidationError):
            SemanticBlock(kind=SemanticKind.FACT, label="l", statement="s", weight=0.5)  # type: ignore[call-arg]

    def test_unsafe_integer_is_refused(self) -> None:
        with pytest.raises((ValidationError, NonDeterministicValueError)):
            CanonicalBlock(blob=OciDigest.of(PDF), media_type="application/pdf", size=2**53)

    def test_blocks_are_frozen(self) -> None:
        block = semantic()
        with pytest.raises(ValidationError):
            block.label = "something else"  # type: ignore[misc]


class TestDecoding:
    """Stored bytes must decode back, and must be refused when they are not canonical."""

    @pytest.mark.parametrize(
        "block",
        [
            semantic(),
            CanonicalBlock(blob=OciDigest.of(PDF), media_type="application/pdf", size=len(PDF)),
            EpisodicBlock(summary="a class", occurred_at="2026-05-14T14:00:00Z"),
            ProceduralBlock(label="l", goal="g", steps=[Step(action="a")]),
            ProvenanceBlock(record=RegistrationRecord(block=BlockId.of(PDF), actor=ACTOR, at="2026-07-24T09:30:00Z")),
        ],
        ids=lambda block: block.MEMORY_TYPE.value,
    )
    def test_round_trips_every_memory_type(self, block: Block) -> None:
        recovered = Block.decode(block.canonical_bytes())
        assert recovered == block
        assert recovered.block_id == block.block_id

    def test_non_canonical_bytes_are_refused(self) -> None:
        loose = json.dumps(json.loads(semantic().canonical_bytes()), indent=2).encode()
        with pytest.raises(BlockIntegrityError, match="not in canonical"):
            Block.decode(loose)

    def test_reordered_keys_are_refused(self) -> None:
        """JCS fixes the key order, so bytes in another order are not the canonical form."""
        envelope = json.loads(semantic().canonical_bytes())
        reordered = json.dumps({key: envelope[key] for key in reversed(list(envelope))}, separators=(",", ":"))
        with pytest.raises(BlockIntegrityError):
            Block.decode(reordered.encode())

    @pytest.mark.parametrize(
        ("mutation", "match"),
        [
            (
                lambda data: data.replace(
                    b'"label":"Fourier series"',
                    b'"label":"Fourier series","label":"chosen last"',
                ),
                "duplicate JSON key",
            ),
            (lambda data: data.replace(b'"Fourier series"', b'"\xff"'), "UTF-8"),
            (lambda data: data.replace(b'"Fourier series"', b'"\\ud800"'), "surrogate"),
        ],
    )
    def test_ambiguous_json_is_refused_before_it_can_define_identity(self, mutation, match: str) -> None:
        with pytest.raises(BlockSchemaError, match=match):
            Block.decode(mutation(semantic().canonical_bytes()))

    @pytest.mark.parametrize(
        ("data", "match"),
        [
            (b"not json", "not valid JSON"),
            (b"[]", "must be an object"),
            (b'{"boltzmann":1}', "malformed block envelope"),
        ],
    )
    def test_malformed_envelopes_are_refused(self, data: bytes, match: str) -> None:
        with pytest.raises(BlockSchemaError, match=match):
            Block.decode(data)

    def test_unknown_protocol_version_is_refused(self) -> None:
        envelope = json.loads(semantic().canonical_bytes())
        envelope["boltzmann"] = 99
        with pytest.raises(BlockSchemaError, match="protocol version"):
            Block.decode(json.dumps(envelope, separators=(",", ":")).encode())

    def test_unknown_memory_type_is_refused(self) -> None:
        envelope = json.loads(semantic().canonical_bytes())
        envelope["memory_type"] = "mythical"
        with pytest.raises(BlockSchemaError, match="unknown memory type"):
            Block.decode(json.dumps(envelope, separators=(",", ":")).encode())

    def test_unknown_schema_version_is_refused(self) -> None:
        envelope = json.loads(semantic().canonical_bytes())
        envelope["schema_version"] = 7
        with pytest.raises(BlockSchemaError, match="no schema registered"):
            Block.decode(json.dumps(envelope, separators=(",", ":")).encode())

    def test_unknown_serialization_is_refused(self) -> None:
        envelope = json.loads(semantic().canonical_bytes())
        envelope["serialization"] = "dag-cbor/1"
        with pytest.raises(BlockSchemaError, match="declares serialization"):
            Block.decode(json.dumps(envelope, separators=(",", ":")).encode())


class TestRegistry:
    """One schema per memory type and version, so decoding is unambiguous."""

    def test_all_five_memory_types_are_registered(self) -> None:
        registered = {memory_type for memory_type, _ in Block.registry()}
        assert registered == set(MemoryType)

    def test_registry_maps_to_the_expected_classes(self) -> None:
        registry = Block.registry()
        assert registry[(MemoryType.CANONICAL, 1)] is CanonicalBlock
        assert registry[(MemoryType.SEMANTIC, 1)] is SemanticBlock
        assert registry[(MemoryType.PROVENANCE, 1)] is ProvenanceBlock


class TestCanonicalBlock:
    """Canonical evidence is a statement about bytes, and nothing else."""

    def test_payload_holds_only_bytes_facts(self) -> None:
        block = CanonicalBlock(blob=OciDigest.of(PDF), media_type="application/pdf", size=len(PDF))
        assert set(block.payload()) == {"blob", "media_type", "size"}

    def test_identity_depends_only_on_the_bytes_described(self) -> None:
        """Two actors registering the same source compute the same block, so dedup is a real no-op."""
        first = CanonicalBlock(blob=OciDigest.of(PDF), media_type="application/pdf", size=len(PDF))
        second = CanonicalBlock(blob=OciDigest.of(PDF), media_type="application/pdf", size=len(PDF))
        assert first.block_id == second.block_id

    def test_actor_and_timestamp_cannot_be_placed_on_the_block(self) -> None:
        """They live in provenance; putting them here would break dedup."""
        with pytest.raises(ValidationError):
            CanonicalBlock(  # type: ignore[call-arg]
                blob=OciDigest.of(PDF),
                media_type="application/pdf",
                size=len(PDF),
                actor=ACTOR,
                at="2026-07-24T09:30:00Z",
            )

    def test_normalized_view_changes_the_identity(self) -> None:
        bare = CanonicalBlock(blob=OciDigest.of(PDF), media_type="application/pdf", size=len(PDF))
        with_view = CanonicalBlock(
            blob=OciDigest.of(PDF),
            media_type="application/pdf",
            size=len(PDF),
            normalized_view=NormalizedView(blob=OciDigest.of(b"plain text"), media_type="text/plain", size=10),
        )
        assert bare.block_id != with_view.block_id

    def test_blob_is_a_physical_digest_not_a_block_id(self) -> None:
        """Observed bytes are a transportable file, not a unit of knowledge."""
        with pytest.raises(ValidationError):
            CanonicalBlock(blob=BlockId.of(PDF), media_type="application/pdf", size=len(PDF))


class TestEpisodicBlock:
    """The chronological record."""

    def test_interval_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError, match="precedes"):
            EpisodicBlock(
                summary="a class",
                occurred_at="2026-05-14T16:00:00Z",
                ended_at="2026-05-14T14:00:00Z",
            )

    def test_non_canonical_timestamp_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            EpisodicBlock(summary="a class", occurred_at="2026-05-14T14:00:00+00:00")

    def test_episodic_module_is_append_only(self) -> None:
        assert MemoryType.EPISODIC.is_append_only
        assert not MemoryType.EPISODIC.is_droppable


class TestProvenanceRecords:
    """The ledger, including the entries that make a removal auditable."""

    def test_derivation_requires_evidence(self) -> None:
        """A derived block with no evidence has no root to be audited against."""
        with pytest.raises(ValidationError):
            DerivationRecord(
                block=BlockId.of(b"derived"),
                derived_from=[],
                producer=Producer(kind=ProducerKind.MODEL, id="some-model"),
                actor=ACTOR,
                at="2026-07-24T09:30:00Z",
            )

    def test_removal_requires_a_reason(self) -> None:
        with pytest.raises(ValidationError):
            RemovalRecord(
                blocks=[BlockId.of(b"wrong")],
                mechanism=RemovalMechanism.DROP,
                memory_type=MemoryType.SEMANTIC,
                actor=ACTOR,
                at="2026-07-24T09:30:00Z",
                reason="",
            )

    def test_records_are_discriminated_by_type(self) -> None:
        block = ProvenanceBlock(
            record=RemovalRecord(
                blocks=[BlockId.of(b"wrong")],
                mechanism=RemovalMechanism.DROP,
                memory_type=MemoryType.SEMANTIC,
                actor=ACTOR,
                at="2026-07-24T09:30:00Z",
                reason="incorrect definition",
            )
        )
        decoded = Block.decode(block.canonical_bytes())
        assert isinstance(decoded, ProvenanceBlock)
        assert decoded.record.record_type == "removal"

    @pytest.mark.parametrize(
        ("mechanism", "is_redaction"),
        [
            (RemovalMechanism.DROP, False),
            (RemovalMechanism.SUPERSEDE, False),
            (RemovalMechanism.PRUNE, False),
            (RemovalMechanism.TOMBSTONE, True),
            (RemovalMechanism.CRYPTO_SHRED, True),
            (RemovalMechanism.LINEAGE_REWRITE, True),
        ],
    )
    def test_redaction_is_distinguished_from_exclusion(self, mechanism: RemovalMechanism, is_redaction: bool) -> None:
        assert mechanism.is_redaction is is_redaction


class TestSemanticBlock:
    """Relations live on the block, so the graph index can be rebuilt from them."""

    def test_relations_are_part_of_the_identity(self) -> None:
        target = BlockId.of(b"other concept")
        bare = semantic()
        linked = semantic(relations=[Relation(predicate="depends_on", target=target)])
        assert bare.block_id != linked.block_id

    def test_relation_targets_are_block_ids(self) -> None:
        with pytest.raises(ValidationError):
            Relation(predicate="depends_on", target=OciDigest.of(b"a file"))
