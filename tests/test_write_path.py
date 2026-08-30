"""The write path end to end: register, delegate, validate, commit.

This is the lifecycle of Section 11, exercised against a real store. The external model is a stub,
because the SDK embeds none and a test should not pretend otherwise.
"""

from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    DerivationRecord,
    NormalizationRecord,
    Producer,
    ProducerKind,
    RegistrationRecord,
    SupersessionRecord,
    ValidationRecord,
)
from boltzmann.brain import HEAD_POINTER, Brain, BrainState
from boltzmann.exceptions import ProtocolError, SnapshotError
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.pipelines import register_pipeline
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.ingest.task import ProcessingTask
from boltzmann.ingest.validation import ValidationStatus
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.memory import MemoryBlockStore

PDF = b"%PDF-1.7 Lecture 07: a periodic function decomposes into sines and cosines."
CURATOR = Actor(id="curator", kind=ActorKind.HUMAN, name="Example Curator")
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="2026-07")


@pytest.fixture
def brain() -> Brain:
    return Brain(MemoryBlockStore(), actor=CURATOR)


@pytest.fixture
def request_() -> RegistrationRequest:
    return RegistrationRequest(
        media_type="application/pdf",
        actor=CURATOR,
        origin="https://example.edu/l07.pdf",
        license="CC-BY-4.0",
    )


def semantic_candidate(source: BlockId, label: str = "Fourier series", statement: str = "f(x) = ...") -> Candidate:
    return Candidate(
        memory_type=MemoryType.SEMANTIC,
        evidence=[source],
        locator="p.147",
        payload={"kind": "formula", "label": label, "statement": statement, "subject": "signals"},
    )


def proposals(*candidates: Candidate) -> CandidateSet:
    return CandidateSet(producer=MODEL, candidates=list(candidates))


class PlainTextPipeline:
    """A deterministic transform, as an implementer would supply."""

    name = "test-pdf-to-text"
    version = "1.0.0"
    output_media_type = "text/plain"

    def accepts(self, media_type: str) -> bool:
        return media_type == "application/pdf"

    def normalize(self, data: bytes) -> bytes:
        return data.removeprefix(b"%PDF-1.7 ").strip()


class UpperPipeline:
    """Accepts anything, so the normalized view can be checked independently of media type."""

    name = "test-upper"
    version = "1.0.0"
    output_media_type = "text/plain"

    def accepts(self, media_type: str) -> bool:
        return True

    def normalize(self, data: bytes) -> bytes:
        return b"NORMALIZED"


# The registry is process-global and refuses a name collision, so register once.
register_pipeline(PlainTextPipeline())
register_pipeline(UpperPipeline())


class TestRegister:
    """Preserve the source. Do not declare it true."""

    def test_advances_the_canonical_root(self, brain: Brain, request_: RegistrationRequest) -> None:
        assert not brain.snapshot().has_module(MemoryType.CANONICAL)
        result = brain.register(PDF, request_)
        assert result.commit is not None
        assert brain.root_of(MemoryType.CANONICAL) == result.commit.roots[MemoryType.CANONICAL]
        assert brain.module(MemoryType.CANONICAL).block_ids == [result.block_id]

    def test_records_who_when_and_under_what_policy(self, brain: Brain, request_: RegistrationRequest) -> None:
        """§5's actor, timestamp and licence live in provenance, which is what keeps dedup a no-op."""
        result = brain.register(PDF, request_)
        records = [block.record for block in brain.module(MemoryType.PROVENANCE).blocks()]
        registrations = [record for record in records if isinstance(record, RegistrationRecord)]
        assert len(registrations) == 1
        assert registrations[0].block == result.block_id
        assert registrations[0].actor == CURATOR
        assert registrations[0].origin == "https://example.edu/l07.pdf"
        assert registrations[0].license == "CC-BY-4.0"

    def test_the_canonical_block_carries_only_bytes_facts(self, brain: Brain, request_: RegistrationRequest) -> None:
        result = brain.register(PDF, request_)
        block = brain.module(MemoryType.CANONICAL).get(result.block_id)
        assert set(block.payload()) == {"blob", "media_type", "size"}
        assert block.size == len(PDF)

    def test_re_registering_identical_bytes_is_a_noop(self, brain: Brain, request_: RegistrationRequest) -> None:
        """§8.1 step 3. Nothing new is stored and no snapshot is published."""
        first = brain.register(PDF, request_)
        before = brain.snapshot()

        second = brain.register(PDF, request_)
        assert second.block_id == first.block_id
        assert second.duplicate
        assert second.commit is None
        assert brain.snapshot() == before

    def test_a_second_actor_registering_the_same_source_gets_the_same_identity(
        self, brain: Brain, request_: RegistrationRequest
    ) -> None:
        first = brain.register(PDF, request_)
        other = RegistrationRequest(media_type="application/pdf", actor=Actor(id="sam", kind=ActorKind.AGENT))
        assert brain.register(PDF, other).block_id == first.block_id

    def test_different_bytes_are_different_evidence(self, brain: Brain, request_: RegistrationRequest) -> None:
        first = brain.register(PDF, request_)
        second = brain.register(PDF + b" appendix", request_)
        assert second.block_id != first.block_id
        assert len(brain.module(MemoryType.CANONICAL)) == 2


class TestNormalizedViews:
    """A view is evidence only if the transform that made it is reproducible."""

    def test_the_view_is_addressed_and_recorded(self, brain: Brain) -> None:
        request = RegistrationRequest(media_type="application/pdf", actor=CURATOR, normalize_with="test-pdf-to-text")
        result = brain.register(PDF, request)
        block = brain.module(MemoryType.CANONICAL).get(result.block_id)

        assert block.normalized_view is not None
        assert block.normalized_view.media_type == "text/plain"
        assert brain.store.get_bytes(block.normalized_view.blob).startswith(b"Lecture 07")

        records = [b.record for b in brain.module(MemoryType.PROVENANCE).blocks()]
        normalizations = [r for r in records if isinstance(r, NormalizationRecord)]
        assert len(normalizations) == 1
        assert normalizations[0].block == result.block_id
        assert (normalizations[0].pipeline, normalizations[0].pipeline_version) == ("test-pdf-to-text", "1.0.0")

    def test_the_view_changes_the_canonical_identity(self, brain: Brain, request_: RegistrationRequest) -> None:
        bare = brain.register(PDF, request_)
        with_view = brain.register(
            PDF, RegistrationRequest(media_type="application/pdf", actor=CURATOR, normalize_with="test-pdf-to-text")
        )
        assert with_view.block_id != bare.block_id

    def test_a_pipeline_that_does_not_accept_the_media_type_is_refused(self, brain: Brain) -> None:
        request = RegistrationRequest(media_type="image/png", actor=CURATOR, normalize_with="test-pdf-to-text")
        with pytest.raises(ProtocolError, match="does not accept"):
            brain.register(PDF, request)


class TestReplace:
    """Register plus a supersession edge. Never a mutation of stored bytes."""

    def test_records_the_edge_and_keeps_both_editions(self, brain: Brain, request_: RegistrationRequest) -> None:
        first = brain.register(PDF, request_)
        second = brain.replace(PDF + b" (corrected)", request_, supersedes=first.block_id)

        canonical = brain.module(MemoryType.CANONICAL)
        assert {first.block_id, second.block_id} == set(canonical.block_ids)

        records = [b.record for b in brain.module(MemoryType.PROVENANCE).blocks()]
        edges = [r for r in records if isinstance(r, SupersessionRecord)]
        assert len(edges) == 1
        assert (edges[0].supersedes, edges[0].block) == (first.block_id, second.block_id)

    def test_superseding_something_absent_is_refused(self, brain: Brain, request_: RegistrationRequest) -> None:
        with pytest.raises(ProtocolError, match="nothing for the new edition"):
            brain.replace(PDF, request_, supersedes=BlockId.of(b"never registered"))

    def test_superseding_with_identical_bytes_is_refused(self, brain: Brain, request_: RegistrationRequest) -> None:
        first = brain.register(PDF, request_)
        with pytest.raises(ProtocolError, match="byte-identical"):
            brain.replace(PDF, request_, supersedes=first.block_id)


class TestDefineTask:
    """What the protocol asks the model, and what it refuses to ask."""

    def test_defaults_to_the_proposable_types(self, brain: Brain, request_: RegistrationRequest) -> None:
        source = brain.register(PDF, request_).block_id
        task = brain.define_task(source)
        assert task.source == source
        assert set(task.allowed_memory_types) == {
            MemoryType.EPISODIC,
            MemoryType.SEMANTIC,
            MemoryType.PROCEDURAL,
        }
        assert task.output_schema == "boltzmann.candidates/v1"

    def test_a_task_over_absent_evidence_is_refused(self, brain: Brain) -> None:
        with pytest.raises(ProtocolError, match="not in the canonical composition"):
            brain.define_task(BlockId.of(b"never registered"))

    def test_canonical_and_provenance_cannot_be_requested(self, brain: Brain, request_: RegistrationRequest) -> None:
        source = brain.register(PDF, request_).block_id
        for forbidden in (MemoryType.CANONICAL, MemoryType.PROVENANCE):
            with pytest.raises(ValueError, match="cannot allow"):
                brain.define_task(source, allowed=[forbidden])


class TestValidate:
    """The gate. Nothing is stored by validating."""

    @pytest.fixture
    def source(self, brain: Brain, request_: RegistrationRequest) -> BlockId:
        return brain.register(PDF, request_).block_id

    def test_a_clean_proposal_is_validated_and_typed(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        report = brain.validate(proposals(semantic_candidate(source)), task)
        assert report.is_clean
        assert report.results[0].block is not None
        assert report.results[0].block.label == "Fourier series"

    def test_validating_stores_nothing(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        before = brain.snapshot()
        brain.validate(proposals(semantic_candidate(source)), task)
        assert brain.snapshot() == before

    def test_evidence_that_is_not_installed_is_rejected(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        invented = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[BlockId.of(b"a source never registered")],
            payload={"kind": "fact", "label": "Invented", "statement": "..."},
        )
        result = brain.validate(proposals(invented), task).results[0]
        assert result.status is ValidationStatus.REJECTED
        assert result.issues[0].code == "evidence-not-found"
        assert result.block is None

    def test_a_type_the_task_did_not_allow_is_rejected(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source, allowed=[MemoryType.SEMANTIC])
        episode = Candidate(
            memory_type=MemoryType.EPISODIC,
            evidence=[source],
            payload={"summary": "a class", "occurred_at": "2026-05-14T14:00:00Z"},
        )
        result = brain.validate(proposals(episode), task).results[0]
        assert result.status is ValidationStatus.REJECTED
        assert result.issues[0].code == "memory-type-not-allowed"

    def test_a_malformed_payload_is_rejected(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        broken = Candidate(memory_type=MemoryType.SEMANTIC, evidence=[source], payload={"kind": "formula"})
        result = brain.validate(proposals(broken), task).results[0]
        assert result.status is ValidationStatus.REJECTED
        assert result.issues[0].code == "schema"

    def test_a_float_in_a_payload_is_rejected(self, brain: Brain, source: BlockId) -> None:
        """The gate refuses what could never be hashed identically by two clients."""
        task = brain.define_task(source)
        floaty = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={"kind": "fact", "label": "L", "statement": "S", "aliases": [1.5]},
        )
        assert brain.validate(proposals(floaty), task).results[0].status is ValidationStatus.REJECTED

    def test_a_duplicate_is_rejected(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        candidate = semantic_candidate(source)
        brain.commit(brain.validate(proposals(candidate), task))

        result = brain.validate(proposals(candidate), task).results[0]
        assert result.status is ValidationStatus.REJECTED
        assert result.issues[0].code == "duplicate"

    def test_a_conflicting_statement_is_contradicted_not_rejected(self, brain: Brain, source: BlockId) -> None:
        """A contradiction is information; what to do with it is a policy decision."""
        task = brain.define_task(source)
        brain.commit(brain.validate(proposals(semantic_candidate(source)), task))

        conflicting = semantic_candidate(source, statement="f(x) = something else entirely")
        result = brain.validate(proposals(conflicting), task).results[0]
        assert result.status is ValidationStatus.CONTRADICTED
        assert result.issues[0].code == "contradiction"

    def test_a_dangling_relation_is_rejected(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        dangling = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={
                "kind": "concept",
                "label": "L",
                "statement": "S",
                "relations": [{"predicate": "depends_on", "target": str(BlockId.of(b"absent"))}],
            },
        )
        result = brain.validate(proposals(dangling), task).results[0]
        assert result.status is ValidationStatus.REJECTED
        assert result.issues[0].code == "relation-target-not-found"

    def test_the_producer_is_carried_through(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source, allowed=[MemoryType.SEMANTIC])
        report = brain.validate(proposals(semantic_candidate(source)), task)
        assert report.producer == MODEL


class TestCommit:
    """The only write path."""

    @pytest.fixture
    def source(self, brain: Brain, request_: RegistrationRequest) -> BlockId:
        return brain.register(PDF, request_).block_id

    def test_commits_only_validated_candidates(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        good = semantic_candidate(source)
        bad = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[BlockId.of(b"absent")],
            payload={"kind": "fact", "label": "L", "statement": "S"},
        )
        result = brain.commit(brain.validate(proposals(good, bad), task))
        assert len(result.committed) == 1
        assert len(brain.module(MemoryType.SEMANTIC)) == 1

    def test_nothing_committable_publishes_no_snapshot(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        before = brain.snapshot()
        bad = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[BlockId.of(b"absent")],
            payload={"kind": "fact", "label": "L", "statement": "S"},
        )
        result = brain.commit(brain.validate(proposals(bad), task))
        assert result.is_empty
        assert brain.snapshot() == before

    def test_advances_every_touched_module_in_one_commit(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        episode = Candidate(
            memory_type=MemoryType.EPISODIC,
            evidence=[source],
            payload={"summary": "Lecture 07", "occurred_at": "2026-05-14T14:00:00Z"},
        )
        result = brain.commit(brain.validate(proposals(semantic_candidate(source), episode), task))
        assert set(result.roots) == {MemoryType.SEMANTIC, MemoryType.EPISODIC, MemoryType.PROVENANCE}

    def test_writes_a_derivation_edge_per_block(self, brain: Brain, source: BlockId) -> None:
        """The edge a canonical drop walks to build its dependency closure."""
        task = brain.define_task(source)
        result = brain.commit(brain.validate(proposals(semantic_candidate(source)), task))

        records = [b.record for b in brain.module(MemoryType.PROVENANCE).blocks()]
        derivations = [r for r in records if isinstance(r, DerivationRecord)]
        assert len(derivations) == 1
        assert derivations[0].block == result.committed[0]
        assert derivations[0].derived_from == [source]
        assert derivations[0].producer == MODEL
        assert derivations[0].locator == "p.147"

    def test_writes_the_verdict_that_admitted_each_block(self, brain: Brain, source: BlockId) -> None:
        """Otherwise "it was validated" is a claim a consumer has to take from whoever committed."""
        task = brain.define_task(source)
        result = brain.commit(brain.validate(proposals(semantic_candidate(source)), task))

        records = [b.record for b in brain.module(MemoryType.PROVENANCE).blocks()]
        validations = [r for r in records if isinstance(r, ValidationRecord)]
        assert len(validations) == 1
        assert validations[0].block == result.committed[0]
        assert validations[0].verdict is ValidationStatus.VALIDATED
        assert validations[0].task == task.task_id
        assert validations[0].actor == CURATOR

    def test_the_verdict_names_the_checks_that_produced_it(self, brain: Brain, source: BlockId) -> None:
        """The same VALIDATED under two different check sets says two different things."""
        task = brain.define_task(source)
        report = brain.validate(proposals(semantic_candidate(source)), task)
        brain.commit(report)

        records = [b.record for b in brain.module(MemoryType.PROVENANCE).blocks()]
        validation = next(r for r in records if isinstance(r, ValidationRecord))
        assert validation.checks == report.checks
        assert len(validation.checks) > 1

    def test_the_verdict_is_readable_back_through_the_ledger(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        result = brain.commit(brain.validate(proposals(semantic_candidate(source)), task))

        audit = brain.audit_validation()
        assert audit.is_complete
        assert audit.accounted[MemoryType.SEMANTIC] == [result.committed[0]]

    def test_a_block_committed_without_a_record_audits_as_unaccounted(self, brain: Brain, source: BlockId) -> None:
        """The audit reports; it never refuses. A brain written before the record existed still reads."""
        task = brain.define_task(source)
        report = brain.validate(proposals(semantic_candidate(source)), task)
        committed = report.committable[0].block
        assert committed is not None

        # The write path without the verdict: what an SDK that predates the record produced.
        brain._write(blocks={MemoryType.SEMANTIC: [committed]}, provenance=[])

        audit = brain.audit_validation()
        assert not audit.is_complete
        assert audit.unaccounted[MemoryType.SEMANTIC] == [committed.block_id]
        assert brain.verify()

    def test_provenance_is_not_counted_as_committed_knowledge(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        result = brain.commit(brain.validate(proposals(semantic_candidate(source)), task))
        assert len(result.committed) == 1
        # One derivation and one validation: what produced the block, and what admitted it.
        assert len(result.provenance) == 2
        assert set(result.committed).isdisjoint(result.provenance)

    def test_a_committed_block_proves_into_its_root(self, brain: Brain, source: BlockId) -> None:
        task = brain.define_task(source)
        result = brain.commit(brain.validate(proposals(semantic_candidate(source)), task))
        block_id = result.committed[0]
        assert brain.prove(block_id, MemoryType.SEMANTIC).verify(brain.root_of(MemoryType.SEMANTIC))

    def test_falls_back_to_the_client_as_producer(self, brain: Brain, source: BlockId) -> None:
        """A proposer that declares nothing still leaves an auditable trail."""
        task = brain.define_task(source)
        anonymous = CandidateSet(candidates=[semantic_candidate(source)])
        brain.commit(brain.validate(anonymous, task))

        records = [b.record for b in brain.module(MemoryType.PROVENANCE).blocks()]
        derivation = next(r for r in records if isinstance(r, DerivationRecord))
        assert derivation.producer.kind is ProducerKind.ACTOR
        assert derivation.producer.id == CURATOR.id


class TestIngest:
    """The whole path in one call."""

    def test_registers_delegates_validates_and_commits(self, brain: Brain, request_: RegistrationRequest) -> None:
        seen: dict[str, object] = {}

        def proposer(task: ProcessingTask, source: bytes) -> CandidateSet:
            seen["task"] = task
            seen["source"] = source
            return proposals(semantic_candidate(task.source))

        result = brain.ingest(PDF, request_, proposer)
        assert seen["source"] == PDF
        assert isinstance(seen["task"], ProcessingTask)
        assert len(result.committed) == 1
        assert brain.verify()

    def test_the_proposer_sees_the_normalized_view_when_there_is_one(self, brain: Brain) -> None:
        seen: dict[str, bytes] = {}

        def proposer(task: ProcessingTask, source: bytes) -> CandidateSet:
            seen["source"] = source
            return proposals(semantic_candidate(task.source))

        request = RegistrationRequest(media_type="application/pdf", actor=CURATOR, normalize_with="test-upper")
        brain.ingest(PDF, request, proposer)
        assert seen["source"] == b"NORMALIZED"


class TestPersistence:
    """A snapshot has to be enough to reopen the brain."""

    def test_reopening_recovers_the_same_roots(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        first = Brain.open(tmp_path / "brain", actor=CURATOR)
        source = first.register(PDF, request_).block_id
        task = first.define_task(source)
        first.commit(first.validate(proposals(semantic_candidate(source)), task))
        expected = first.snapshot()

        reopened = Brain.open(tmp_path / "brain", actor=CURATOR)
        assert reopened.snapshot() == expected
        assert reopened.verify()
        assert reopened.module(MemoryType.SEMANTIC).block_ids == first.module(MemoryType.SEMANTIC).block_ids

    def test_a_fresh_directory_is_an_empty_brain(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        assert brain.snapshot().installed == []
        assert brain.verify()
        assert brain.state() == {}

    def test_snapshots_chain_and_are_retained(self, brain: Brain, request_: RegistrationRequest) -> None:
        source = brain.register(PDF, request_).block_id
        task = brain.define_task(source)
        brain.commit(brain.validate(proposals(semantic_candidate(source)), task))

        history = brain.history()
        assert len(history) == 2
        assert history[0].first_parent == history[1].digest

    def test_retention_is_capped_by_policy(self, request_: RegistrationRequest) -> None:
        from boltzmann.retention.policy import RetentionPolicy

        brain = Brain(MemoryBlockStore(), actor=CURATOR, policy=RetentionPolicy(retained_roots=2))
        for index in range(4):
            brain.register(PDF + str(index).encode(), request_)
        assert len(brain.history()) == 2

    def test_the_head_pointer_is_not_content(self, tmp_path: Path, request_: RegistrationRequest) -> None:
        """Content is immutable; which snapshot is current is the one mutable cell."""
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        brain.register(PDF, request_)
        raw = brain.store.read_pointer(HEAD_POINTER)
        assert raw is not None
        state = BrainState.model_validate_json(raw)
        assert state.snapshot == brain.snapshot().digest

    def test_a_composition_that_disagrees_with_its_root_is_refused(
        self, brain: Brain, request_: RegistrationRequest
    ) -> None:
        """A stored leaf list that does not reproduce its root is corruption, not a version."""
        brain.register(PDF, request_)
        reference = brain.snapshot().modules[MemoryType.CANONICAL]
        forged = Snapshot.of(
            [
                reference.model_copy(
                    update={
                        # A well-formed, canonical composition -- of the wrong version. The point is
                        # that it does not reproduce the root the reference files it under.
                        "composition": brain.store.put_bytes(Composition(MemoryType.CANONICAL).document())
                    }
                )
            ]
        )
        tampered = Brain(brain.store, actor=CURATOR, snapshot=forged)
        with pytest.raises(SnapshotError, match="snapshot files it under"):
            tampered.module(MemoryType.CANONICAL)


class TestResolve:
    """Resolution goes through membership, not just the store."""

    def test_resolves_across_installed_modules(self, brain: Brain, request_: RegistrationRequest) -> None:
        source = brain.register(PDF, request_).block_id
        task = brain.define_task(source)
        committed = brain.commit(brain.validate(proposals(semantic_candidate(source)), task)).committed[0]

        assert brain.resolve(source).media_type == "application/pdf"
        assert brain.resolve(committed).label == "Fourier series"

    def test_a_block_in_the_store_but_in_no_composition_is_not_resolvable(self, brain: Brain) -> None:
        from boltzmann.blocks.semantic import SemanticBlock, SemanticKind

        orphan = SemanticBlock(kind=SemanticKind.FACT, label="orphan", statement="never committed")
        brain.store.put_block(orphan)
        assert brain.store.has(orphan.block_id)
        with pytest.raises(Exception, match="not in any installed composition"):
            brain.resolve(orphan.block_id)
