"""Retention: how knowledge leaves a brain, in the four mechanisms Section 10 keeps distinct.

The scenarios follow the paper's own examples, because they are the ones that carry the design: the
wrong lecture PDF whose derived definitions must go with it, the superseded block that stays a member,
and the redacted block whose membership still verifies while its bytes are gone.
"""

from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    DemotionRecord,
    Producer,
    ProducerKind,
    RemovalMechanism,
    RemovalRecord,
    SupersessionRecord,
)
from boltzmann.brain import Brain
from boltzmann.distribution.manifest import parse_manifest
from boltzmann.exceptions import AppendOnlyViolationError, ProtocolError, RetentionPolicyError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.module.ledger import Ledger
from boltzmann.query.request import Query
from boltzmann.retention.policy import PERMISSIVE_POLICY, RetentionPolicy
from boltzmann.retention.reachability import mark, sweep
from boltzmann.retention.requests import DropRequest, ProducerDropRequest
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator@example.org", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="2026-07")
OTHER_MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="2026-05")
REQUEST = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
REDACTABLE = RetentionPolicy(canonical_drop_allowed=True, redactable_media_types=["application/pdf"])


def fact(source: BlockId, label: str, producer: Producer = MODEL, **extra: object) -> CandidateSet:
    return CandidateSet(
        producer=producer,
        candidates=[
            Candidate(
                memory_type=MemoryType.SEMANTIC,
                evidence=[source],
                payload={"kind": "fact", "label": label, "statement": f"about {label}", **extra},
            )
        ],
    )


def commit(brain: Brain, source: BlockId, label: str, producer: Producer = MODEL, **extra: object) -> BlockId:
    task = brain.define_task(source)
    report = brain.validate(fact(source, label, producer, **extra), task)
    assert report.is_clean, [issue.detail for r in report.results for issue in r.issues]
    return brain.commit(report).committed[0]


def seeded(path: Path) -> Brain:
    """A brain on disk with one source and one derived fact, so it has something to pack."""
    brain = Brain.open(path, actor=CURATOR, policy=PERMISSIVE_POLICY)
    source = brain.register(b"%PDF-1.7 lecture 07", REQUEST).block_id
    commit(brain, source, "Fourier")
    return brain


@pytest.fixture
def brain() -> Brain:
    return Brain(MemoryBlockStore(), actor=CURATOR, policy=PERMISSIVE_POLICY)


@pytest.fixture
def wrong_pdf(brain: Brain) -> BlockId:
    """The paper's example: a source ingested in error, with derived definitions citing it."""
    source = brain.register(b"%PDF-1.7 the wrong lecture", REQUEST).block_id
    commit(brain, source, "A")
    commit(brain, source, "B")
    return source


class TestDropExcludes:
    """A drop rewrites the composition. It never mutates a block."""

    def test_the_block_leaves_the_composition_and_the_root_moves(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        before = brain.root_of(MemoryType.SEMANTIC)

        result = brain.drop(
            DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="incorrect")
        )
        assert victim not in brain.module(MemoryType.SEMANTIC)
        assert brain.root_of(MemoryType.SEMANTIC) != before
        assert result.dropped[MemoryType.SEMANTIC] == [victim]

    def test_the_block_itself_is_untouched(self, brain: Brain) -> None:
        """Blocks stay content-addressed and immutable; only membership changed."""
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="incorrect"))
        assert brain.store.is_resolvable(victim)
        assert brain.store.get_block(victim).block_id == victim

    def test_a_dropped_block_no_longer_resolves_through_the_brain(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="incorrect"))
        with pytest.raises(Exception, match="not in any installed composition"):
            brain.resolve(victim)

    def test_a_dropped_block_disappears_from_search(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        assert len(brain.search(Query(text="wrong"))) == 1
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="incorrect"))
        assert len(brain.search(Query(text="wrong"))) == 0

    def test_the_brain_still_verifies_after_a_drop(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="incorrect"))
        assert brain.verify()

    def test_the_removal_is_recorded(self, brain: Brain) -> None:
        """Principle 8: every removal is explicit, recorded, and reportable."""
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(
            DropRequest(
                blocks=[victim],
                memory_type=MemoryType.SEMANTIC,
                actor=CURATOR,
                reason="a wrong definition",
                policy_name="editorial",
            )
        )
        records = [
            block.record
            for block in brain.module(MemoryType.PROVENANCE).blocks()
            if isinstance(block.record, RemovalRecord)
        ]
        assert len(records) == 1
        assert records[0].blocks == [victim]
        assert records[0].mechanism is RemovalMechanism.DROP
        assert records[0].reason == "a wrong definition"
        assert records[0].policy == "editorial"
        assert records[0].actor == CURATOR

    def test_dropping_a_block_that_is_not_a_member_is_refused(self, brain: Brain) -> None:
        brain.register(b"%PDF-1.7 lecture", REQUEST)
        with pytest.raises(ProtocolError, match="not in its composition"):
            brain.drop(
                DropRequest(
                    blocks=[BlockId.of(b"never committed")],
                    memory_type=MemoryType.CANONICAL,
                    actor=CURATOR,
                    reason="x",
                )
            )


class TestPrivilegedCanonicalCascade:
    """Section 10.3's example, which is the reason the cascade exists."""

    def test_dropping_evidence_drops_what_cited_it(self, brain: Brain, wrong_pdf: BlockId) -> None:
        assert len(brain.module(MemoryType.SEMANTIC)) == 2

        result = brain.drop(
            DropRequest(blocks=[wrong_pdf], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="ingested in error")
        )
        assert result.dropped[MemoryType.CANONICAL] == [wrong_pdf]
        assert len(result.dropped[MemoryType.SEMANTIC]) == 2
        assert len(brain.module(MemoryType.SEMANTIC)) == 0
        assert brain.verify()

    def test_the_plan_reports_the_cascade_before_anything_is_written(self, brain: Brain, wrong_pdf: BlockId) -> None:
        before = brain.snapshot()
        plan = brain.plan_drop(
            DropRequest(blocks=[wrong_pdf], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x")
        )
        assert plan.privileged
        assert plan.size == 2
        assert brain.snapshot() == before

    def test_it_publishes_several_roots_in_one_commit(self, brain: Brain, wrong_pdf: BlockId) -> None:
        """One logical removal of evidence advances every module it reached, as one version."""
        result = brain.drop(
            DropRequest(blocks=[wrong_pdf], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x")
        )
        assert {MemoryType.CANONICAL, MemoryType.SEMANTIC, MemoryType.PROVENANCE} <= set(result.roots)
        assert result.snapshot.first_parent is not None

    def test_knowledge_from_other_evidence_survives(self, brain: Brain, wrong_pdf: BlockId) -> None:
        good = brain.register(b"%PDF-1.7 the right lecture", REQUEST).block_id
        commit(brain, good, "C")

        brain.drop(DropRequest(blocks=[wrong_pdf], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x"))
        assert [block.label for block in brain.module(MemoryType.SEMANTIC).blocks()] == ["C"]

    def test_the_cascade_is_marked_as_such_in_the_ledger(self, brain: Brain, wrong_pdf: BlockId) -> None:
        brain.drop(DropRequest(blocks=[wrong_pdf], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x"))
        records = [
            block.record
            for block in brain.module(MemoryType.PROVENANCE).blocks()
            if isinstance(block.record, RemovalRecord)
        ]
        cascaded = [record for record in records if record.cascaded_from is not None]
        assert len(cascaded) == 1
        assert cascaded[0].cascaded_from == wrong_pdf
        assert cascaded[0].memory_type is MemoryType.SEMANTIC

    def test_re_derivable_dependents_are_reported(self, brain: Brain, wrong_pdf: BlockId) -> None:
        """Re-derivation is never the default; the plan just says what could be regenerated."""
        replacement = brain.register(b"%PDF-1.7 the right lecture", REQUEST).block_id
        plan = brain.plan_drop(
            DropRequest(
                blocks=[wrong_pdf],
                memory_type=MemoryType.CANONICAL,
                actor=CURATOR,
                reason="x",
                rederive_against=replacement,
            )
        )
        assert len(plan.rederivable) == 2
        assert set(plan.rederivable) == set(plan.dependents[MemoryType.SEMANTIC])

    def test_without_a_replacement_nothing_is_re_derivable(self, brain: Brain, wrong_pdf: BlockId) -> None:
        plan = brain.plan_drop(
            DropRequest(blocks=[wrong_pdf], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x")
        )
        assert plan.rederivable == []


class TestStructuralCascade:
    """Derived blocks cite canonical evidence, but they reference each other structurally."""

    def test_dropping_a_concept_drops_what_relates_to_it(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        target = commit(brain, source, "Periodicity")
        dependent = commit(
            brain,
            source,
            "Fourier",
            relations=[{"predicate": "depends_on", "target": str(target)}],
        )

        result = brain.drop(
            DropRequest(blocks=[target], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="wrong")
        )
        assert set(result.dropped[MemoryType.SEMANTIC]) == {target, dependent}
        assert len(brain.module(MemoryType.SEMANTIC)) == 0

    def test_dropping_a_concept_drops_a_procedure_that_uses_it(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        formula = commit(brain, source, "Fourier")
        task = brain.define_task(source)
        procedure = brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.PROCEDURAL,
                            evidence=[source],
                            payload={
                                "label": "Compute",
                                "goal": "coefficients",
                                "steps": [{"action": "apply", "uses": [str(formula)]}],
                            },
                        )
                    ],
                ),
                task,
            )
        ).committed[0]

        result = brain.drop(
            DropRequest(blocks=[formula], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="wrong")
        )
        assert result.dropped[MemoryType.PROCEDURAL] == [procedure]
        assert len(brain.module(MemoryType.PROCEDURAL)) == 0

    def test_the_cascade_is_transitive(self, brain: Brain) -> None:
        """Otherwise a drop would leave a dangling reference one hop further out."""
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        first = commit(brain, source, "First")
        second = commit(brain, source, "Second", relations=[{"predicate": "uses", "target": str(first)}])
        third = commit(brain, source, "Third", relations=[{"predicate": "uses", "target": str(second)}])

        result = brain.drop(DropRequest(blocks=[first], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="wrong"))
        assert set(result.dropped[MemoryType.SEMANTIC]) == {first, second, third}


class TestBatchInvalidation:
    """Section 10.3: state a drop over everything one producer made."""

    def test_drops_everything_a_model_version_derived(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        bad_one = commit(brain, source, "bad one", producer=MODEL)
        bad_two = commit(brain, source, "bad two", producer=MODEL)
        good = commit(brain, source, "good", producer=OTHER_MODEL)

        brain.drop_by_producer(
            ProducerDropRequest(
                producer=MODEL,
                memory_types=[MemoryType.SEMANTIC],
                actor=CURATOR,
                reason="the model was misconfigured",
            )
        )
        remaining = set(brain.module(MemoryType.SEMANTIC).block_ids)
        assert remaining == {good}
        assert not remaining & {bad_one, bad_two}

    def test_a_producer_without_a_version_matches_every_version(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        commit(brain, source, "from july", producer=MODEL)
        commit(brain, source, "from may", producer=OTHER_MODEL)

        brain.drop_by_producer(
            ProducerDropRequest(
                producer=Producer(kind=ProducerKind.MODEL, id="some-model"),
                memory_types=[MemoryType.SEMANTIC],
                actor=CURATOR,
                reason="every version was wrong",
            )
        )
        assert len(brain.module(MemoryType.SEMANTIC)) == 0

    def test_a_producer_that_made_nothing_changes_nothing(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        commit(brain, source, "kept")
        before = brain.snapshot()

        result = brain.drop_by_producer(
            ProducerDropRequest(
                producer=Producer(kind=ProducerKind.MODEL, id="never-ran"),
                memory_types=[MemoryType.SEMANTIC],
                actor=CURATOR,
                reason="x",
            )
        )
        assert result.dropped == {}
        assert brain.snapshot() == before


class TestPolicy:
    """Policy is configuration; two refusals are not."""

    def test_the_episodic_module_refuses_to_drop(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        episode = brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.EPISODIC,
                            evidence=[source],
                            payload={"summary": "a class", "occurred_at": "2026-05-14T14:00:00Z"},
                        )
                    ],
                ),
                task,
            )
        ).committed[0]

        with pytest.raises(RetentionPolicyError, match="no policy can permit"):
            brain.drop(DropRequest(blocks=[episode], memory_type=MemoryType.EPISODIC, actor=CURATOR, reason="x"))

    def test_a_canonical_drop_is_refused_by_the_default_policy(self) -> None:
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        with pytest.raises(RetentionPolicyError, match="privileged"):
            brain.drop(DropRequest(blocks=[source], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x"))

    def test_a_large_cascade_is_held_for_review(self) -> None:
        policy = RetentionPolicy(canonical_drop_allowed=True, cascade_review_threshold=1)
        brain = Brain(MemoryBlockStore(), actor=CURATOR, policy=policy)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        commit(brain, source, "A")
        commit(brain, source, "B")
        before = brain.snapshot()

        result = brain.drop(DropRequest(blocks=[source], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x"))
        assert result.review_required
        assert result.dropped == {}
        assert brain.snapshot() == before

    def test_a_cascade_under_the_threshold_proceeds(self) -> None:
        policy = RetentionPolicy(canonical_drop_allowed=True, cascade_review_threshold=5)
        brain = Brain(MemoryBlockStore(), actor=CURATOR, policy=policy)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        commit(brain, source, "A")

        result = brain.drop(DropRequest(blocks=[source], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x"))
        assert not result.review_required
        assert len(brain.module(MemoryType.SEMANTIC)) == 0

    def test_a_cascade_cannot_rewrite_an_append_only_module(self) -> None:
        """A canonical drop must not reach the episodic module through the back door."""
        brain = Brain(MemoryBlockStore(), actor=CURATOR, policy=PERMISSIVE_POLICY)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.EPISODIC,
                            evidence=[source],
                            payload={"summary": "a class", "occurred_at": "2026-05-14T14:00:00Z"},
                        )
                    ],
                ),
                task,
            )
        )
        with pytest.raises((RetentionPolicyError, AppendOnlyViolationError)):
            brain.drop(DropRequest(blocks=[source], memory_type=MemoryType.CANONICAL, actor=CURATOR, reason="x"))


class TestSupersession:
    """Accessibility changes; membership does not."""

    def test_the_superseded_block_stays_a_member(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        old = commit(brain, source, "old")
        new = commit(brain, source, "new")
        root = brain.root_of(MemoryType.SEMANTIC)

        brain.supersede(new, old, MemoryType.SEMANTIC, reason="better wording")
        assert old in brain.module(MemoryType.SEMANTIC)
        assert brain.root_of(MemoryType.SEMANTIC) == root
        assert brain.prove(old, MemoryType.SEMANTIC).verify(root)

    def test_the_superseded_block_is_held_back_from_search(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        old = commit(brain, source, "old")
        new = commit(brain, source, "new")
        brain.supersede(new, old, MemoryType.SEMANTIC)

        visible = {match.block_id for match in brain.search(Query(text="about")).matches}
        assert visible == {new}
        with_history = {
            match.block_id for match in brain.search(Query(text="about", filters={"include_superseded": True})).matches
        }
        assert with_history == {old, new}

    def test_the_edge_is_recorded(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        old = commit(brain, source, "old")
        new = commit(brain, source, "new")
        brain.supersede(new, old, MemoryType.SEMANTIC, reason="better wording")

        edges = [
            block.record
            for block in brain.module(MemoryType.PROVENANCE).blocks()
            if isinstance(block.record, SupersessionRecord)
        ]
        assert len(edges) == 1
        assert (edges[0].block, edges[0].supersedes) == (new, old)
        assert edges[0].reason == "better wording"

    def test_a_block_cannot_supersede_itself(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        block = commit(brain, source, "one")
        with pytest.raises(ProtocolError, match="cannot supersede itself"):
            brain.supersede(block, block, MemoryType.SEMANTIC)

    def test_superseding_works_on_the_episodic_module(self, brain: Brain) -> None:
        """It is the only removal path an append-only module has."""
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        task = brain.define_task(source)
        episodes = brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.EPISODIC,
                            evidence=[source],
                            payload={"summary": f"class {index}", "occurred_at": "2026-05-14T14:00:00Z"},
                        )
                        for index in range(2)
                    ],
                ),
                task,
            )
        ).committed
        brain.supersede(episodes[1], episodes[0], MemoryType.EPISODIC, reason="corrected")
        assert episodes[0] in brain.module(MemoryType.EPISODIC)


class TestDemotion:
    """Lowering priority without removing, recorded in the ledger rather than on the block."""

    def test_a_demoted_block_stays_a_member_and_still_proves(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        block = commit(brain, source, "noisy")
        root = brain.root_of(MemoryType.SEMANTIC)

        brain.demote(block, MemoryType.SEMANTIC, reason="too noisy")
        assert block in brain.module(MemoryType.SEMANTIC)
        assert brain.root_of(MemoryType.SEMANTIC) == root
        assert brain.prove(block, MemoryType.SEMANTIC).verify(root)

    def test_a_demoted_block_is_held_back_from_search(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        block = commit(brain, source, "noisy")
        assert len(brain.search(Query(text="noisy"))) == 1

        brain.demote(block, MemoryType.SEMANTIC)
        assert len(brain.search(Query(text="noisy"))) == 0
        assert len(brain.search(Query(text="noisy", filters={"include_superseded": True}))) == 1

    def test_demoting_does_not_change_the_block(self, brain: Brain) -> None:
        """A block is immutable, so accessibility cannot live on it or the identity would change."""
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        block = commit(brain, source, "noisy")
        brain.demote(block, MemoryType.SEMANTIC)
        assert brain.store.get_block(block).block_id == block

    def test_the_decision_is_recorded(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        block = commit(brain, source, "noisy")
        brain.demote(block, MemoryType.SEMANTIC, reason="too noisy")

        records = [
            entry.record
            for entry in brain.module(MemoryType.PROVENANCE).blocks()
            if isinstance(entry.record, DemotionRecord)
        ]
        assert len(records) == 1
        assert records[0].block == block
        assert records[0].reason == "too noisy"


class TestPrune:
    """Reclaim what no retained root needs. Never decide what to forget."""

    def make(self, tmp_path: Path, retained: int = 1) -> Brain:
        policy = RetentionPolicy(retained_roots=retained, canonical_drop_allowed=True)
        return Brain.open(tmp_path / "brain", actor=CURATOR, policy=policy)

    def test_a_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        brain = self.make(tmp_path)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="x"))

        held = len(list(brain.store.iter_digests()))
        report = brain.prune(dry_run=True)
        assert report.dry_run
        assert report.reclaimed_count > 0
        assert len(list(brain.store.iter_digests())) == held

    def test_a_dropped_block_becomes_reclaimable(self, tmp_path: Path) -> None:
        brain = self.make(tmp_path)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="x"))

        report = brain.prune(dry_run=False)
        assert victim.hex in {digest.hex for digest in report.reclaimed}
        assert not brain.store.has(victim)

    def test_what_a_retained_root_needs_survives(self, tmp_path: Path) -> None:
        brain = self.make(tmp_path)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        kept = commit(brain, source, "kept")
        brain.prune(dry_run=False)

        assert brain.store.is_resolvable(kept)
        assert brain.store.is_resolvable(source)
        assert brain.verify()

    def test_the_observed_bytes_of_a_kept_source_survive(self, tmp_path: Path) -> None:
        """A source blob is named by a canonical block, not by a composition, so reachability has to
        follow that hop or pruning would destroy the evidence."""
        brain = self.make(tmp_path)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        blob = brain.module(MemoryType.CANONICAL).get(source).blob
        brain.prune(dry_run=False)
        assert brain.store.is_resolvable(blob)

    def test_pruning_twice_reclaims_nothing_the_second_time(self, tmp_path: Path) -> None:
        brain = self.make(tmp_path)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="x"))
        brain.prune(dry_run=False)
        assert brain.prune(dry_run=False).reclaimed_count == 0

    def test_a_wider_retention_keeps_more(self, tmp_path: Path) -> None:
        wide = self.make(tmp_path / "wide", retained=50)
        source = wide.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(wide, source, "wrong")
        wide.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="x"))
        assert wide.prune(dry_run=True).reclaimed_count == 0

    def test_mark_and_sweep_partition_the_store(self, tmp_path: Path) -> None:
        brain = self.make(tmp_path)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        commit(brain, source, "kept")

        keep = mark(brain.history(), brain.store)
        reclaimable = sweep(keep, brain.store)
        held = {digest.hex for digest in brain.store.iter_digests()}
        assert not {digest.hex for digest in reclaimable} & keep
        assert {digest.hex for digest in reclaimable} <= held


class TestRedaction:
    """Destroy bytes a retained root still names, and report it as such."""

    def make(self) -> tuple[Brain, BlockId]:
        brain = Brain(MemoryBlockStore(), actor=CURATOR, policy=REDACTABLE)
        source = brain.register(b"%PDF-1.7 personal data", REQUEST).block_id
        return brain, source

    def test_membership_still_verifies_after_the_bytes_are_gone(self) -> None:
        """The DAG references identities, not bytes, so no hash changed."""
        brain, source = self.make()
        root = brain.root_of(MemoryType.CANONICAL)
        brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")

        assert source in brain.module(MemoryType.CANONICAL)
        assert brain.root_of(MemoryType.CANONICAL) == root
        assert brain.prove(source, MemoryType.CANONICAL).verify(root)

    def test_redaction_grows_the_signed_tombstone_set_in_one_successor(self) -> None:
        brain, source = self.make()
        before = brain.snapshot()

        result = brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")

        reference = result.snapshot.modules[MemoryType.CANONICAL]
        assert reference.tombstones == [source]
        assert reference.root == before.modules[MemoryType.CANONICAL].root
        assert result.snapshot.parents == [before.digest]
        assert result.snapshot.digest != before.digest

    def test_a_later_write_preserves_tombstones_the_composition_still_names(self) -> None:
        brain, source = self.make()
        brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")

        brain.register(b"%PDF-1.7 public data", REQUEST)

        assert brain.snapshot().modules[MemoryType.CANONICAL].tombstones == [source]

    def test_the_observed_bytes_are_destroyed_too(self) -> None:
        """Redacting the descriptor and leaving the source readable would redact nothing."""
        brain, source = self.make()
        blob = brain.module(MemoryType.CANONICAL).get(source).blob
        result = brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")

        assert blob in result.redacted
        assert not brain.store.is_resolvable(blob)

    def test_it_is_reported_as_tombstoned_not_missing(self) -> None:
        """Section 10.6: a removed block must never be indistinguishable from a corrupted one."""
        brain, source = self.make()
        brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")

        report = brain.resolvability()
        assert source in report.tombstoned[MemoryType.CANONICAL]
        assert source not in report.resolvable.get(MemoryType.CANONICAL, [])
        assert report.is_intact

    def test_the_erasure_is_recorded_before_the_bytes_go(self) -> None:
        brain, source = self.make()
        result = brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")

        assert result.provenance
        records = [
            entry.record
            for entry in brain.module(MemoryType.PROVENANCE).blocks()
            if isinstance(entry.record, RemovalRecord)
        ]
        assert records[0].mechanism is RemovalMechanism.TOMBSTONE
        assert records[0].reason == "GDPR art. 17"

    def test_prior_roots_are_not_invalidated(self) -> None:
        brain, source = self.make()
        result = brain.redact(source, MemoryType.CANONICAL, reason="GDPR art. 17")
        assert not result.invalidates_prior_roots

    def test_redaction_is_refused_without_an_explicit_policy(self) -> None:
        """Wrong knowledge is dropped, not redacted, so this needs opting in."""
        brain = Brain(MemoryBlockStore(), actor=CURATOR, policy=PERMISSIVE_POLICY)
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        with pytest.raises(RetentionPolicyError, match="dropped, not redacted"):
            brain.redact(source, MemoryType.CANONICAL, reason="x")


class TestLedger:
    """The reverse indices both the query path and the cascade read."""

    def test_dependents_inverts_derived_from(self, brain: Brain, wrong_pdf: BlockId) -> None:
        ledger = Ledger.of(brain.modules())
        assert len(ledger.dependents[wrong_pdf]) == 2
        assert ledger.closure(wrong_pdf) == ledger.dependents[wrong_pdf]

    def test_producers_are_recorded_per_block(self, brain: Brain, wrong_pdf: BlockId) -> None:
        ledger = Ledger.of(brain.modules())
        assert len(ledger.made_by(MODEL)) == 2
        assert ledger.made_by(OTHER_MODEL) == set()

    def test_a_block_with_no_ledger_entry_is_accessible(self, brain: Brain, wrong_pdf: BlockId) -> None:
        ledger = Ledger.of(brain.modules())
        assert ledger.is_accessible(wrong_pdf)

    def test_removals_are_recorded(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        victim = commit(brain, source, "wrong")
        brain.drop(DropRequest(blocks=[victim], memory_type=MemoryType.SEMANTIC, actor=CURATOR, reason="x"))
        assert victim in Ledger.of(brain.modules()).removed

    def test_a_brain_without_provenance_yields_an_empty_ledger(self) -> None:
        assert Ledger.of({}).dependents == {}


class TestTagsAreRootsToo:
    """A layout has two kinds of root, and only one of them was being honoured.

    Snapshots name knowledge. They do not name the *artifact* built from it -- the manifest and the packed
    layer per module -- and those are exactly what a tag names. Pruning without counting the tags left
    index.json pointing at a manifest whose bytes had been reclaimed: a layout claiming a tag it could no
    longer serve, which no OCI tool can read and which this SDK cannot reopen either.
    """

    def test_packing_then_pruning_keeps_the_manifest(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        manifest = brain.pack(tag="v1")

        brain.prune(dry_run=False)

        assert brain.store.is_resolvable(manifest.digest)
        assert parse_manifest(brain.store.get_bytes(manifest.digest)) == manifest

    def test_packing_then_pruning_keeps_every_layer(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        manifest = brain.pack(tag="v1")

        brain.prune(dry_run=False)

        for descriptor in [manifest.config, *manifest.layers]:
            assert brain.store.is_resolvable(descriptor.digest), descriptor.media_type

    def test_the_index_entry_still_resolves_after_pruning(self, tmp_path: Path) -> None:
        """The symptom as a reader meets it: follow index.json and find the bytes."""
        brain = seeded(tmp_path / "brain")
        brain.pack(tag="v1")
        brain.prune(dry_run=False)

        entries = brain.store.index()["manifests"]
        assert entries
        for entry in entries:
            digest = OciDigest.parse(entry["digest"])
            assert brain.store.is_resolvable(digest)
            assert len(brain.store.get_bytes(digest)) == entry["size"]

    def test_a_replaced_tag_lets_its_old_manifest_go(self, tmp_path: Path) -> None:
        """Only what the tags name *now* is kept, or a brain would accumulate every manifest it ever wrote."""
        brain = seeded(tmp_path / "brain")
        first = brain.pack(tag="v1")

        commit(brain, brain.module(MemoryType.CANONICAL).block_ids[0], "Laplace")
        second = brain.pack(tag="v1")
        assert first.digest != second.digest

        brain.prune(dry_run=False)

        assert brain.store.is_resolvable(second.digest)
        assert not brain.store.is_resolvable(first.digest)

    def test_a_store_without_a_layout_index_prunes_as_before(self, brain: Brain) -> None:
        source = brain.register(b"%PDF-1.7 lecture", REQUEST).block_id
        commit(brain, source, "A")
        assert brain.prune(dry_run=True).dry_run
