"""The wire formats: shapes two clients must agree on to hand work to the same model.

A wire format is not an implementation detail, so these are checked against the shapes the paper
prints rather than against whatever the SDK happens to produce.
"""

import json

import pytest
from pydantic import ValidationError

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import CANDIDATES_SCHEMA
from boltzmann.distribution.media_types import (
    ANNOTATION_MEMORY_TYPE,
    ANNOTATION_MERKLE_ROOT,
    ARTIFACT_TYPE,
    memory_type_of,
    module_media_type,
)
from boltzmann.identity.digest import BlockId, MerkleRoot
from boltzmann.ingest.proposer import Candidate, CandidateProposer, CandidateSet
from boltzmann.ingest.task import ProcessingTask, TaskOperation
from boltzmann.module.snapshot import Snapshot
from boltzmann.query.evidence import EvidenceBundle, Match, SourceRef
from boltzmann.query.request import Query, RetrievalMode

SOURCE = BlockId.of(b"%PDF-1.7 lecture notes")


class TestProcessingTask:
    """Section 8.2 prints this shape; it must round-trip through JSON unchanged."""

    def build(self) -> ProcessingTask:
        return ProcessingTask(
            operation=TaskOperation.EXTRACT_KNOWLEDGE,
            source=SOURCE,
            allowed_memory_types=[MemoryType.EPISODIC, MemoryType.SEMANTIC, MemoryType.PROCEDURAL],
            requirements=["cite source ranges", "do not invent"],
        )

    def test_matches_the_documented_shape(self) -> None:
        payload = json.loads(self.build().model_dump_json(exclude_none=True))
        assert payload == {
            "operation": "extract_knowledge",
            "source": str(SOURCE),
            "allowed_memory_types": ["episodic", "semantic", "procedural"],
            "requirements": ["cite source ranges", "do not invent"],
            "output_schema": CANDIDATES_SCHEMA,
        }

    def test_round_trips(self) -> None:
        task = self.build()
        assert ProcessingTask.model_validate_json(task.model_dump_json()) == task

    def test_source_is_a_block_id(self) -> None:
        with pytest.raises(ValidationError):
            ProcessingTask(
                operation=TaskOperation.EXTRACT_KNOWLEDGE,
                source=MerkleRoot.of(b"a snapshot"),
                allowed_memory_types=[MemoryType.SEMANTIC],
            )

    def test_at_least_one_memory_type_is_required(self) -> None:
        with pytest.raises(ValidationError):
            ProcessingTask(operation=TaskOperation.EXTRACT_KNOWLEDGE, source=SOURCE, allowed_memory_types=[])

    def test_rederive_is_a_named_operation(self) -> None:
        """Re-derivation against a replacement canonical is explicit, never implicit."""
        assert TaskOperation.REDERIVE.value == "rederive"


class TestCandidates:
    """``boltzmann.candidates/v1``: what a proposer returns, and what it cannot."""

    def candidate(self) -> Candidate:
        return Candidate(
            memory_type=MemoryType.SEMANTIC,
            payload={"kind": "formula", "label": "Fourier series", "statement": "f(x) = ..."},
            evidence=[SOURCE],
            locator="p. 147",
        )

    def test_declares_its_schema(self) -> None:
        assert CandidateSet().schema_version == CANDIDATES_SCHEMA

    def test_round_trips(self) -> None:
        proposal = CandidateSet(task_id="task-1", candidates=[self.candidate()])
        assert CandidateSet.model_validate_json(proposal.model_dump_json()) == proposal

    def test_evidence_is_required(self) -> None:
        """A proposal with no evidence has no root to be audited against."""
        with pytest.raises(ValidationError):
            Candidate(memory_type=MemoryType.SEMANTIC, payload={}, evidence=[])

    def test_confidence_is_a_string(self) -> None:
        """Floats are forbidden anywhere that feeds a hash, so a model's estimate is text."""
        assert Candidate.model_fields["confidence"].annotation == str | None

    def test_selecting_by_type(self) -> None:
        proposal = CandidateSet(
            candidates=[
                self.candidate(),
                Candidate(memory_type=MemoryType.PROCEDURAL, payload={}, evidence=[SOURCE]),
            ]
        )
        assert len(proposal) == 2
        assert len(proposal.of_type(MemoryType.SEMANTIC)) == 1
        assert proposal.of_type(MemoryType.EPISODIC) == []

    def test_a_callable_satisfies_the_proposer_interface(self) -> None:
        """The SDK ships no proposer; anything with this shape is one."""

        class Stub:
            def __call__(self, task: ProcessingTask, source: bytes) -> CandidateSet:
                return CandidateSet()

        assert isinstance(Stub(), CandidateProposer)


class TestEvidenceBundle:
    """Section 9.3 prints this shape. It carries data and provenance, and no answer."""

    def build(self) -> EvidenceBundle:
        concept = BlockId.of(b"concept-1")
        return EvidenceBundle(
            matches=[
                Match(
                    block_id=concept,
                    memory_type=MemoryType.SEMANTIC,
                    content={"kind": "formula", "label": "Fourier series"},
                    score="0.91",
                    sources=[SourceRef(block_id=SOURCE, locator="147")],
                    verified=True,
                )
            ],
            verified_against={MemoryType.SEMANTIC: MerkleRoot.of(b"semantic root")},
        )

    def test_matches_the_documented_shape(self) -> None:
        match = json.loads(self.build().model_dump_json(exclude_none=True))["matches"][0]
        assert match["block_id"].startswith("sha256:")
        assert match["memory_type"] == "semantic"
        assert match["verified"] is True
        assert match["sources"][0]["locator"] == "147"

    def test_round_trips(self) -> None:
        bundle = self.build()
        assert EvidenceBundle.model_validate_json(bundle.model_dump_json()) == bundle

    def test_verification_can_be_rechecked(self) -> None:
        bundle = self.build()
        assert bundle.all_verified
        bundle.require_verified()
        assert bundle.verified_against[MemoryType.SEMANTIC] == MerkleRoot.of(b"semantic root")

    def test_an_empty_bundle_is_valid(self) -> None:
        """No match is a legitimate answer; it is not an error."""
        empty = EvidenceBundle()
        assert len(empty) == 0
        assert empty.all_verified
        empty.require_verified()


class TestQuery:
    """A declarative request, with hints a planner may ignore."""

    def test_defaults_are_conservative(self) -> None:
        query = Query(text="decompose a periodic function into sines")
        assert query.hints.mode is RetrievalMode.AUTO
        assert query.limit == 10
        assert query.filters.include_superseded is False

    def test_round_trips(self) -> None:
        query = Query(text="Fourier", filters={"memory_types": [MemoryType.SEMANTIC], "subject": "signals"})
        assert Query.model_validate_json(query.model_dump_json()) == query

    def test_a_filter_only_query_is_valid(self) -> None:
        """ "The episodes of last May" carries no terms, and refusing it would make recency filters
        unusable on their own."""
        query = Query(filters={"since": "2026-05-01T00:00:00Z"})
        assert query.is_filter_only
        assert Query.model_validate_json(query.model_dump_json()) == query

    def test_unknown_field_is_refused(self) -> None:
        """``extra="forbid"`` is what keeps an index name from sneaking in as a filter."""
        with pytest.raises(ValidationError):
            Query(text="Fourier", index="vector")  # type: ignore[call-arg]


class TestMediaTypes:
    """What two registries must agree on to exchange a brain."""

    def test_artifact_type_is_versioned(self) -> None:
        assert ARTIFACT_TYPE.endswith(".v1+json")
        assert "boltzmann.brain" in ARTIFACT_TYPE

    @pytest.mark.parametrize("memory_type", list(MemoryType))
    def test_every_module_has_its_own_layer_type(self, memory_type: MemoryType) -> None:
        """One blob per module is what makes selective installation possible."""
        media_type = module_media_type(memory_type)
        assert memory_type.value in media_type
        assert memory_type_of(media_type) is memory_type

    def test_module_media_types_are_all_distinct(self) -> None:
        assert len({module_media_type(kind) for kind in MemoryType}) == len(MemoryType)

    def test_a_non_module_media_type_maps_to_nothing(self) -> None:
        assert memory_type_of("application/vnd.oci.image.layer.v1.tar") is None

    def test_the_merkle_root_annotation_is_separate_from_the_digest(self) -> None:
        """The digest identifies the file; the root identifies the composition inside it."""
        assert ANNOTATION_MERKLE_ROOT != ANNOTATION_MEMORY_TYPE
        assert ANNOTATION_MERKLE_ROOT.startswith("ai.gaussia.boltzmann.")


class TestSnapshotLineage:
    """A snapshot is the config blob of a published artifact, so how it names its parents is wire format.

    One parent is written as the scalar ``parent`` and two or more as the list ``parents``. That is the rule
    Section 6.6 states for block schemas -- a version is a statement, not a preference, so a document is
    written under the oldest form that can express it -- applied to the snapshot document. The consequence
    is the point: a linear history keeps the exact bytes it had before reconciliation existed, and a client
    that has no notion of a second parent stops being able to read a brain only at the point where that
    brain genuinely reconciled something.
    """

    def test_a_root_snapshot_names_no_parent(self) -> None:
        assert b"parent" not in Snapshot().canonical_bytes()

    def test_one_parent_is_written_as_a_scalar(self) -> None:
        linear = Snapshot().with_modules([])
        document = json.loads(linear.canonical_bytes())

        assert isinstance(document["parent"], str)
        assert "parents" not in document

    def test_two_parents_are_written_as_a_list(self) -> None:
        ours = Snapshot().with_modules([])
        merged = ours.reconciled([], [Snapshot(labels={"side": "theirs"}).digest])
        document = json.loads(merged.canonical_bytes())

        assert isinstance(document["parents"], list)
        assert len(document["parents"]) == 2
        assert "parent" not in document

    def test_a_scalar_parent_is_still_readable(self) -> None:
        """Every snapshot published under the scalar form is immutable and still a valid statement of
        composition; refusing it would make existing histories unreadable to gain nothing."""
        linear = Snapshot().with_modules([])
        reread = Snapshot.model_validate_json(linear.canonical_bytes())

        assert reread.parents == linear.parents
        assert reread.digest == linear.digest

    def test_a_document_naming_both_is_refused(self) -> None:
        """It says two things about the same lineage, and there is no reading of it that is not a guess."""
        parent = str(Snapshot().digest)
        with pytest.raises(ValidationError, match="both 'parent' and 'parents'"):
            Snapshot.model_validate({"parent": parent, "parents": [parent]})

    def test_the_same_parent_cannot_be_named_twice(self) -> None:
        parent = str(Snapshot().digest)
        with pytest.raises(ValidationError, match="same parent more than once"):
            Snapshot.model_validate({"parents": [parent, parent]})
