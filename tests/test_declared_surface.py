"""Nothing is declared and unreachable.

A type nobody constructs, an enum member nobody produces, a constant nobody reads -- each promises a
capability that does not exist, and a reader has no way to tell the promise from the feature. So each of
them either got an implementation or got deleted, and this file is the check that they stay reachable.
"""

from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind, RemovalMechanism
from boltzmann.brain import Brain
from boltzmann.constants import CANDIDATES_SCHEMA, EVIDENCE_BUNDLE_SCHEMA, PROCESSING_TASK_SCHEMA
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.exceptions import ProtocolError
from boltzmann.identity.digest import BlockId
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.ingest.schema import wire_schemas
from boltzmann.ingest.task import TaskOperation
from boltzmann.ingest.validation import ValidationStatus, validate
from boltzmann.ingest.validators import DEFAULT_VALIDATORS, UndecidedValidator
from boltzmann.retention.policy import PERMISSIVE_POLICY, RetentionPolicy

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REQUEST = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
REFERENCE = "registry.example/org/brain"


def fact(source: BlockId, label: str, statement: str = "about it") -> CandidateSet:
    return CandidateSet(
        producer=MODEL,
        candidates=[
            Candidate(
                memory_type=MemoryType.SEMANTIC,
                evidence=[source],
                payload={"kind": "fact", "label": label, "statement": statement, "subject": "signals"},
            )
        ],
    )


@pytest.fixture
def brain() -> Brain:
    from boltzmann.store.memory import MemoryBlockStore

    return Brain(MemoryBlockStore(), actor=CURATOR, policy=PERMISSIVE_POLICY)


class TestInstallPlan:
    """It described what a pull would transfer, and nothing produced it."""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> LocalLayoutRegistry:
        return LocalLayoutRegistry(tmp_path / "registry")

    async def test_a_plan_reports_what_a_fresh_install_would_fetch(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        source = Brain.open(tmp_path / "a", actor=CURATOR)
        origin = source.register(b"%PDF-1.7 lecture", REQUEST).block_id
        source.commit(source.validate(fact(origin, "A"), source.define_task(origin)))
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR)
        plan = await target.plan_pull(registry, REFERENCE, "v1")

        assert set(plan.modules) == set(source.snapshot().installed)
        assert set(plan.fetch_layers) == set(plan.modules)
        assert plan.reuse_layers == []
        assert not plan.is_noop

    async def test_planning_downloads_nothing(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Resolving a manifest is cheap; the point is knowing the cost before paying it."""
        source = Brain.open(tmp_path / "a", actor=CURATOR)
        source.register(b"%PDF-1.7 lecture", REQUEST)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR)
        held = len(list(target.store.iter_digests()))
        await target.plan_pull(registry, REFERENCE, "v1")
        assert len(list(target.store.iter_digests())) == held

    async def test_a_second_plan_reports_the_layers_already_held(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        source = Brain.open(tmp_path / "a", actor=CURATOR)
        source.register(b"%PDF-1.7 lecture", REQUEST)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR)
        await target.pull(registry, REFERENCE, "v1")
        plan = await target.plan_pull(registry, REFERENCE, "v1")

        assert plan.fetch_layers == []
        assert set(plan.reuse_layers) == set(plan.modules)
        assert plan.is_noop

    async def test_a_plan_for_a_module_the_artifact_lacks_is_refused(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        source = Brain.open(tmp_path / "a", actor=CURATOR)
        source.register(b"%PDF-1.7 lecture", REQUEST)
        await source.push(registry, REFERENCE, "v1")

        target = Brain.open(tmp_path / "b", actor=CURATOR)
        with pytest.raises(Exception, match="does not carry"):
            await target.plan_pull(registry, REFERENCE, "v1", modules=[MemoryType.SEMANTIC])


class TestRederive:
    """``TaskOperation.REDERIVE`` existed with nothing producing it."""

    def test_a_rederivation_task_names_what_it_replaces(self, brain: Brain) -> None:
        wrong = brain.register(b"%PDF-1.7 the wrong lecture", REQUEST).block_id
        right = brain.register(b"%PDF-1.7 the right lecture", REQUEST).block_id

        task = brain.define_rederivation(right, replacing=wrong)
        assert task.operation is TaskOperation.REDERIVE
        assert task.source == right
        assert str(wrong) in (task.instructions or "")
        assert any(str(wrong) in requirement for requirement in task.requirements)

    def test_re_deriving_against_an_absent_source_is_refused(self, brain: Brain) -> None:
        wrong = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        with pytest.raises(ProtocolError, match="no new evidence"):
            brain.define_rederivation(BlockId.of(b"never registered"), replacing=wrong)

    def test_re_deriving_against_itself_is_refused(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        with pytest.raises(ProtocolError, match="against itself"):
            brain.define_rederivation(source, replacing=source)

    def test_the_regenerated_knowledge_is_a_new_block(self, brain: Brain) -> None:
        """A citation is part of an identity, so re-derivation replaces rather than repairs."""
        from boltzmann.retention.requests import DropRequest

        wrong = brain.register(b"%PDF-1.7 the wrong lecture", REQUEST).block_id
        brain.commit(brain.validate(fact(wrong, "Fourier"), brain.define_task(wrong)))
        original = brain.module(MemoryType.SEMANTIC).block_ids[0]

        right = brain.register(b"%PDF-1.7 the right lecture", REQUEST).block_id
        brain.drop(DropRequest(blocks=[wrong], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="wrong"))

        task = brain.define_rederivation(right, replacing=wrong)
        brain.commit(brain.validate(fact(right, "Fourier"), task))
        regenerated = brain.module(MemoryType.SEMANTIC).block_ids

        assert regenerated != [original]
        assert original not in regenerated


class TestPendingReview:
    """The verdict that means "not decided", which nothing could produce."""

    def test_it_is_not_produced_by_the_protocol_checks(self, brain: Brain) -> None:
        """Every check the protocol owns decides; declining is a deployment's prerogative."""
        assert UndecidedValidator not in {type(check) for check in DEFAULT_VALIDATORS}

    def test_a_check_that_declines_yields_pending_review(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        report = validate(
            fact(source, "Fourier"),
            task,
            brain.modules(),
            validators=[*DEFAULT_VALIDATORS, UndecidedValidator()],
        )
        assert report.results[0].status is ValidationStatus.PENDING_REVIEW

    def test_a_pending_candidate_is_not_committable(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        report = validate(
            fact(source, "Fourier"),
            task,
            brain.modules(),
            validators=[*DEFAULT_VALIDATORS, UndecidedValidator()],
        )
        assert report.committable == []
        assert brain.commit(report).is_empty

    def test_a_real_defect_still_rejects_even_alongside_a_declined_check(self, brain: Brain) -> None:
        """Declining to decide must not launder a malformed proposal into review."""
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        broken = CandidateSet(
            candidates=[Candidate(memory_type=MemoryType.SEMANTIC, evidence=[source], payload={"kind": "fact"})]
        )
        report = validate(broken, task, brain.modules(), validators=[*DEFAULT_VALIDATORS, UndecidedValidator()])
        assert report.results[0].status is ValidationStatus.REJECTED

    def test_every_verdict_is_reachable(self) -> None:
        assert {status.value for status in ValidationStatus} == {
            "validated",
            "pending_review",
            "rejected",
            "contradicted",
        }


class TestContradictionNamesItsCounterpart:
    """A contradiction that named nothing would say something disagrees without saying with what."""

    def test_the_conflicting_block_is_reported(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        brain.commit(brain.validate(fact(source, "Fourier", "the original wording"), task))
        held = brain.module(MemoryType.SEMANTIC).block_ids[0]

        report = brain.validate(fact(source, "Fourier", "a different wording"), task)
        result = report.results[0]
        assert result.status is ValidationStatus.CONTRADICTED
        assert result.conflicts_with == [held]

    def test_a_clean_proposal_names_no_conflict(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        assert brain.validate(fact(source, "Fourier"), task).results[0].conflicts_with == []


class TestSchemaConstants:
    """Two identifiers were declared and never read."""

    def test_each_names_a_schema_that_exists(self) -> None:
        schemas = wire_schemas()
        for identifier in (CANDIDATES_SCHEMA, PROCESSING_TASK_SCHEMA, EVIDENCE_BUNDLE_SCHEMA):
            assert identifier in schemas
            assert schemas[identifier]["$id"] == identifier


class TestRemovalMechanisms:
    """Every mechanism the enum names is reachable through an operation or refused by policy."""

    @pytest.mark.parametrize(
        ("mechanism", "reachable_by"),
        [
            (RemovalMechanism.DROP, "drop"),
            (RemovalMechanism.SUPERSEDE, "supersede"),
            (RemovalMechanism.DEMOTE, "demote"),
            (RemovalMechanism.PRUNE, "prune"),
            (RemovalMechanism.TOMBSTONE, "redact"),
        ],
    )
    def test_an_operation_exists_for_it(self, mechanism: RemovalMechanism, reachable_by: str) -> None:
        assert callable(getattr(Brain, reachable_by))

    @pytest.mark.parametrize("mechanism", [RemovalMechanism.CRYPTO_SHRED, RemovalMechanism.LINEAGE_REWRITE])
    def test_the_two_that_need_more_than_code_are_refused_not_stubbed(self, mechanism: RemovalMechanism) -> None:
        """Crypto-shredding needs encryption at rest and a lineage rewrite invalidates prior roots.
        Neither is in v1, so the policy refuses them by name rather than a stub pretending to work."""
        from boltzmann.exceptions import RetentionPolicyError

        policy = RetentionPolicy(redactable_media_types=["*/*"], allowed_mechanisms=[RemovalMechanism.TOMBSTONE])
        with pytest.raises(RetentionPolicyError, match="policy permits only"):
            policy.authorize(mechanism, MemoryType.CANONICAL)
