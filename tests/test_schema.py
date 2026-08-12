"""The emitted JSON Schema has to be usable, not merely well-formed.

Every test here validates real documents through a real JSON Schema validator, because the point of
emitting a schema is that a model constrained by it produces candidates the gate accepts. A test that
only checked the shape of the schema document would pass while the schema rejected valid input.
"""

import pytest
from jsonschema import Draft202012Validator

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.constants import CANDIDATES_SCHEMA, EVIDENCE_BUNDLE_SCHEMA, PROCESSING_TASK_SCHEMA
from boltzmann.exceptions import BlockSchemaError
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.ingest.schema import (
    block_schema,
    candidates_schema,
    evidence_bundle_schema,
    processing_task_schema,
    wire_schemas,
)
from boltzmann.ingest.task import ProcessingTask, TaskOperation
from boltzmann.query.evidence import EvidenceBundle, Match, SourceRef
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
SOURCE = BlockId.of(b"%PDF-1.7 lecture notes")


def semantic_payload(**overrides: object) -> dict:
    payload = {"kind": "formula", "label": "Fourier series", "statement": "f(x) = ..."}
    payload.update(overrides)  # type: ignore[arg-type]
    return payload


def proposal(*candidates: Candidate) -> dict:
    return CandidateSet(producer=MODEL, candidates=list(candidates)).model_dump(mode="json", exclude_none=True)


def semantic_candidate(**overrides: object) -> Candidate:
    return Candidate(memory_type=MemoryType.SEMANTIC, evidence=[SOURCE], payload=semantic_payload(**overrides))


def _semantic_def() -> str:
    """The ``$defs`` key of the semantic schema the emitted document advertises."""
    versions = sorted(version for kind, version in Block.registry() if kind is MemoryType.SEMANTIC)
    return Block.registry()[(MemoryType.SEMANTIC, versions[-1])].__name__


@pytest.fixture
def validator() -> Draft202012Validator:
    return Draft202012Validator(candidates_schema())


class TestWellFormed:
    """Every emitted schema must be a valid Draft 2020-12 document."""

    @pytest.mark.parametrize(
        "schema",
        [candidates_schema(), processing_task_schema(), evidence_bundle_schema()],
        ids=["candidates", "task", "evidence"],
    )
    def test_the_schema_itself_validates(self, schema: dict) -> None:
        Draft202012Validator.check_schema(schema)

    @pytest.mark.parametrize(
        ("schema", "identifier"),
        [
            (candidates_schema(), CANDIDATES_SCHEMA),
            (processing_task_schema(), PROCESSING_TASK_SCHEMA),
            (evidence_bundle_schema(), EVIDENCE_BUNDLE_SCHEMA),
        ],
        ids=["candidates", "task", "evidence"],
    )
    def test_declares_its_identifier(self, schema: dict, identifier: str) -> None:
        """The name a task cites and the schema behind it have to be the same string."""
        assert schema["$id"] == identifier

    def test_every_wire_format_is_exported(self) -> None:
        assert set(wire_schemas()) == {CANDIDATES_SCHEMA, PROCESSING_TASK_SCHEMA, EVIDENCE_BUNDLE_SCHEMA}

    def test_the_document_is_self_contained(self) -> None:
        """No external $ref, so an implementer can hand it to a model without resolving anything."""
        schema = candidates_schema()
        refs = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                if "$ref" in node:
                    refs.append(node["$ref"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(schema)
        assert refs
        for ref in refs:
            assert ref.startswith("#/$defs/")
            assert ref.removeprefix("#/$defs/") in schema["$defs"], ref


class TestThePayloadIsConstrained:
    """The whole reason to emit this: the payload is where a model most needs guidance."""

    def test_the_payload_is_no_longer_an_open_object(self) -> None:
        """``Candidate.payload`` is dict[str, Any] in Python, which alone tells a model nothing."""
        variant = _variant_for(candidates_schema(), MemoryType.SEMANTIC)
        # Named after whichever class is advertised rather than after a literal, because the schema
        # tracks the newest registered version and pinning the name here would turn every future
        # schema bump into a test failure that says nothing about the property being checked.
        assert variant["properties"]["payload"] == {"$ref": f"#/$defs/{_semantic_def()}"}
        assert variant["properties"]["memory_type"] == {"const": "semantic", "type": "string"}

    def test_the_resolved_payload_states_its_required_fields(self) -> None:
        schema = candidates_schema()
        assert schema["$defs"][_semantic_def()]["required"] == ["kind", "label", "statement"]

    def test_content_is_offered_and_not_required(self) -> None:
        """A newer schema may add an optional field; it must not become mandatory to propose one."""
        definition = candidates_schema()["$defs"][_semantic_def()]
        assert "content" in definition["properties"]
        assert "content" not in definition["required"]

    def test_the_resolved_payload_states_its_enums(self) -> None:
        schema = candidates_schema()
        assert schema["$defs"]["SemanticKind"]["enum"] == [
            "concept",
            "fact",
            "formula",
            "relation",
            "constraint",
        ]

    def test_a_well_formed_proposal_validates(self, validator: Draft202012Validator) -> None:
        assert validator.is_valid(proposal(semantic_candidate()))

    @pytest.mark.parametrize(
        ("mutation", "why"),
        [
            (lambda p: p["candidates"][0]["payload"].pop("statement"), "missing a required field"),
            (lambda p: p["candidates"][0]["payload"].update(kind="invented"), "kind outside the enum"),
            (lambda p: p["candidates"][0]["payload"].update(extra=1), "an unexpected payload field"),
            (lambda p: p["candidates"][0].update(evidence=[]), "no evidence cited"),
            (lambda p: p["candidates"][0].update(evidence=["not-a-digest"]), "a malformed digest"),
            (lambda p: p["candidates"][0].update(memory_type="mythical"), "an unknown memory type"),
            (lambda p: p["candidates"][0].update(confidence=0.9), "a float where a string is required"),
        ],
    )
    def test_rejects_what_the_gate_would_reject(self, validator: Draft202012Validator, mutation, why: str) -> None:
        document = proposal(semantic_candidate())
        mutation(document)
        assert not validator.is_valid(document), why

    @pytest.mark.parametrize("memory_type", sorted(MemoryType))
    def test_a_block_schema_exists_for_every_memory_type(self, memory_type: MemoryType) -> None:
        schema = block_schema(memory_type)
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False

    def test_an_unregistered_memory_type_is_refused(self) -> None:
        class Fake:
            value = "mythical"

        with pytest.raises(BlockSchemaError, match="no schema registered"):
            block_schema(Fake())  # type: ignore[arg-type]


class TestEveryProposableTypeValidates:
    """A model must be able to express every kind of block the task allows."""

    @pytest.mark.parametrize(
        ("memory_type", "payload"),
        [
            (MemoryType.SEMANTIC, semantic_payload()),
            (MemoryType.EPISODIC, {"summary": "a class", "occurred_at": "2026-05-14T14:00:00Z"}),
            (
                MemoryType.PROCEDURAL,
                {"label": "L", "goal": "G", "steps": [{"action": "integrate"}]},
            ),
        ],
        ids=lambda value: getattr(value, "value", ""),
    )
    def test_round_trips_through_the_validator_and_the_gate(
        self, validator: Draft202012Validator, memory_type: MemoryType, payload: dict
    ) -> None:
        """Schema-valid must mean gate-valid, or the schema is worse than useless."""
        candidate = Candidate(memory_type=memory_type, evidence=[SOURCE], payload=payload)
        document = proposal(candidate)
        assert validator.is_valid(document)

        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        registered = brain.register(
            b"%PDF-1.7 lecture notes", RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        ).block_id
        # SOURCE hashes the raw bytes; a canonical block_id hashes the envelope that describes them, so
        # the candidate has to cite the block rather than the blob.
        cited = Candidate(memory_type=memory_type, evidence=[registered], payload=payload)
        document = proposal(cited)
        assert validator.is_valid(document)

        task = brain.define_task(registered)
        report = brain.validate(CandidateSet.model_validate(document), task)
        assert report.is_clean, [issue.detail for r in report.results for issue in r.issues]


class TestTaskRestriction:
    """A task that allows one type yields a schema that admits only that type."""

    def build(self, *allowed: MemoryType) -> ProcessingTask:
        return ProcessingTask(
            operation=TaskOperation.EXTRACT_KNOWLEDGE,
            source=SOURCE,
            allowed_memory_types=list(allowed),
        )

    def test_one_allowed_type_yields_one_variant(self) -> None:
        schema = candidates_schema(self.build(MemoryType.SEMANTIC))
        items = schema["properties"]["candidates"]["items"]
        assert "oneOf" not in items
        assert items["properties"]["memory_type"]["const"] == "semantic"

    def test_a_disallowed_type_is_refused_by_the_schema(self) -> None:
        """So a constrained model cannot even express what the gate would reject."""
        validator = Draft202012Validator(candidates_schema(self.build(MemoryType.SEMANTIC)))
        episode = Candidate(
            memory_type=MemoryType.EPISODIC,
            evidence=[SOURCE],
            payload={"summary": "a class", "occurred_at": "2026-05-14T14:00:00Z"},
        )
        assert not validator.is_valid(proposal(episode))

    def test_two_allowed_types_yield_two_variants(self) -> None:
        schema = candidates_schema(self.build(MemoryType.SEMANTIC, MemoryType.PROCEDURAL))
        variants = schema["properties"]["candidates"]["items"]["oneOf"]
        assert {variant["properties"]["memory_type"]["const"] for variant in variants} == {
            "semantic",
            "procedural",
        }

    def test_no_task_means_every_proposable_type(self) -> None:
        variants = candidates_schema()["properties"]["candidates"]["items"]["oneOf"]
        assert {variant["properties"]["memory_type"]["const"] for variant in variants} == {
            "semantic",
            "episodic",
            "procedural",
        }

    def test_canonical_and_provenance_never_appear(self) -> None:
        """The task cannot allow them, so the schema must not offer them either."""
        variants = candidates_schema()["properties"]["candidates"]["items"]["oneOf"]
        offered = {variant["properties"]["memory_type"]["const"] for variant in variants}
        assert not offered & {"canonical", "provenance"}


class TestOtherWireFormats:
    def test_a_processing_task_validates_against_its_schema(self) -> None:
        validator = Draft202012Validator(processing_task_schema())
        task = ProcessingTask(
            operation=TaskOperation.EXTRACT_KNOWLEDGE,
            source=SOURCE,
            allowed_memory_types=[MemoryType.SEMANTIC],
        )
        assert validator.is_valid(task.model_dump(mode="json", exclude_none=True))

    def test_an_evidence_bundle_validates_against_its_schema(self) -> None:
        validator = Draft202012Validator(evidence_bundle_schema())
        bundle = EvidenceBundle(
            matches=[
                Match(
                    block_id=BlockId.of(b"concept"),
                    memory_type=MemoryType.SEMANTIC,
                    content={"label": "Fourier series"},
                    score="1.00",
                    sources=[SourceRef(block_id=SOURCE, locator="147")],
                    verified=True,
                )
            ],
            verified_against={MemoryType.SEMANTIC: MerkleRoot.of(b"root")},
        )
        assert validator.is_valid(bundle.model_dump(mode="json", exclude_none=True))

    def test_a_bundle_with_prose_is_refused(self) -> None:
        """The bundle has no answer field, and the schema has to say so too."""
        validator = Draft202012Validator(evidence_bundle_schema())
        assert not validator.is_valid({"matches": [], "answer": "the Fourier series is..."})


class TestBrainSurface:
    def test_the_brain_hands_out_the_schema_for_a_task(self) -> None:
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        source = brain.register(
            b"%PDF-1.7 lecture notes", RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        ).block_id
        task = brain.define_task(source, allowed=[MemoryType.SEMANTIC])

        schema = brain.candidates_schema(task)
        assert schema["$id"] == task.output_schema
        Draft202012Validator.check_schema(schema)


def _variant_for(schema: dict, memory_type: MemoryType) -> dict:
    items = schema["properties"]["candidates"]["items"]
    variants = items.get("oneOf", [items])
    return next(v for v in variants if v["properties"]["memory_type"]["const"] == memory_type.value)
