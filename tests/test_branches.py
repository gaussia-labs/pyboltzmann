"""Branches: named pointers, each publishing to a tag of its own (paper Section 7.5).

The motivating failure is a team on one tag: every publish after the first is refused as divergence until
its writer reconciles, so the tag serializes everyone. These tests hold the protocol's answer to that --
a branch per line of work, joined when a writer chooses -- and the guarantees that make it safe: a branch
head is never reclaimed while named, a checkout never loses a head, and a publish another one replaced
is reported rather than silent.
"""

import logging
from pathlib import Path

import pytest

import boltzmann.brain as brain_module
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import REFS_POINTER, Brain
from boltzmann.branches import BRANCH_TAG_PREFIX, DEFAULT_BRANCH, branch_for_tag, tag_for, validate_branch_name
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.distribution.manifest import BrainManifest
from boltzmann.distribution.registry import RegistryTags
from boltzmann.exceptions import (
    BranchError,
    BranchExistsError,
    BranchNotFoundError,
    DivergenceError,
    InvalidBranchNameError,
    LostPublishError,
    ReconciliationHaltedError,
    UnmergedBranchError,
)
from boltzmann.identity.digest import OciDigest
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.reconcile import ReconcileStrategy
from boltzmann.reconcile.resolution import ReconcileState
from boltzmann.retention.policy import RetentionPolicy
from boltzmann.store.base import BlockStore

ANA = Actor(id="ana@example.org", kind=ActorKind.HUMAN)
BETO = Actor(id="beto@example.org", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
REFERENCE = "registry.example/org/brain"
REQUEST = RegistrationRequest(media_type="application/pdf", actor=ANA)


def llm(label: str):
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


def seeded(path: Path, actor: Actor = ANA, policy: RetentionPolicy | None = None) -> Brain:
    brain = Brain.open(path, actor=actor, policy=policy)
    brain.ingest(b"%PDF-1.7 Lecture 07", REQUEST, llm("Fourier"))
    return brain


def add(brain: Brain, label: str) -> None:
    """Commit one more derived fact over the brain's only source."""
    source = brain.module(MemoryType.CANONICAL).block_ids[0]
    task = brain.define_task(source)
    brain.commit(brain.validate(llm(label)(task, b""), task))


def labels(brain: Brain) -> set[str]:
    semantic = brain.module(MemoryType.SEMANTIC)
    return {semantic.get(block).label for block in semantic.block_ids}


@pytest.fixture
def registry(tmp_path: Path) -> LocalLayoutRegistry:
    return LocalLayoutRegistry(tmp_path / "registry")


class TestNames:
    """The name grammar and the tag mapping are fixed by the protocol, so every client reads them alike."""

    @pytest.mark.parametrize(
        ("name", "tag"),
        [("main", "latest"), ("ana/fix-typo", "br.ana.fix-typo"), ("feature_1", "br.feature_1"), ("a/b/c", "br.a.b.c")],
    )
    def test_a_name_maps_to_its_tag_and_back(self, name: str, tag: str) -> None:
        assert tag_for(name) == tag
        assert branch_for_tag(tag) == name

    def test_the_default_branch_follows_the_deployment_default_tag(self) -> None:
        assert tag_for(DEFAULT_BRANCH, default_tag="stable") == "stable"
        assert branch_for_tag("stable", default_tag="stable") == DEFAULT_BRANCH

    @pytest.mark.parametrize("name", ["", "-x", "a.b", "a//b", "a/", "/a", "ä", "a b"])
    def test_an_invalid_name_is_refused(self, name: str) -> None:
        with pytest.raises(InvalidBranchNameError):
            validate_branch_name(name)

    def test_a_name_whose_tag_would_be_too_long_is_refused(self) -> None:
        with pytest.raises(InvalidBranchNameError, match="128"):
            validate_branch_name("a" * (128 - len(BRANCH_TAG_PREFIX) + 1))
        validate_branch_name("a" * (128 - len(BRANCH_TAG_PREFIX)))

    @pytest.mark.parametrize("tag", ["v1", "1.0.0", "br.main", "br.", "br.a..b", "br.-x", "sha256-abc"])
    def test_a_tag_that_names_no_branch_is_left_alone(self, tag: str) -> None:
        """A release tag is not a branch, and ``br.main`` cannot impersonate the default one."""
        assert branch_for_tag(tag) is None


class TestLocalBranches:
    """Create, check out, and delete without a registry anywhere."""

    def test_a_brain_without_refs_has_main_and_writes_nothing_to_say_so(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        assert brain.current_branch() == DEFAULT_BRANCH
        [main] = brain.branches()
        assert (main.name, main.tag, main.current) == ("main", "latest", True)
        assert main.snapshot == brain.snapshot().digest
        assert not brain.store.read_pointer(REFS_POINTER)

    def test_a_fresh_brain_has_no_branch(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=ANA)
        assert brain.branches() == []
        with pytest.raises(BranchError, match="no snapshot"):
            brain.create_branch("x")

    def test_checkout_round_trips_both_heads(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("ana/nyquist", checkout=True)
        add(brain, "Nyquist")
        assert labels(brain) == {"Fourier", "Nyquist"}
        feature_head = brain.snapshot().digest

        brain.checkout("main")
        assert labels(brain) == {"Fourier"}
        assert brain.verify()

        brain.checkout("ana/nyquist")
        assert brain.snapshot().digest == feature_head
        assert labels(brain) == {"Fourier", "Nyquist"}

    def test_the_current_branch_survives_reopening(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("ana/nyquist", checkout=True)
        add(brain, "Nyquist")

        reopened = Brain.open(tmp_path / "brain", actor=ANA)
        assert reopened.current_branch() == "ana/nyquist"
        assert labels(reopened) == {"Fourier", "Nyquist"}

    def test_a_branch_starts_where_it_is_told_to(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        start = brain.snapshot().digest
        add(brain, "Nyquist")

        from_snapshot = brain.create_branch("from-snapshot", at=start)
        from_branch = brain.create_branch("from-branch", at="from-snapshot")
        assert from_snapshot.snapshot == from_branch.snapshot == start
        assert from_snapshot.tag == "br.from-snapshot"

    def test_creating_an_existing_or_misnamed_branch_is_refused(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x")
        with pytest.raises(BranchExistsError):
            brain.create_branch("x")
        with pytest.raises(BranchExistsError):
            brain.create_branch("main")
        with pytest.raises(InvalidBranchNameError):
            brain.create_branch("not.valid")
        with pytest.raises(BranchNotFoundError):
            brain.create_branch("y", at="nowhere")

    def test_checking_out_an_unknown_branch_is_refused(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        with pytest.raises(BranchNotFoundError):
            brain.checkout("nowhere")

    def test_checkout_is_refused_while_a_reconciliation_is_open(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x")
        head = brain.snapshot().digest
        brain._put_reconcile_state(
            ReconcileState(
                theirs=head, ancestor=head, strategy=ReconcileStrategy.MERGE, actor=ANA, reason="r", head=head
            )
        )
        refs_before = brain.store.read_pointer(REFS_POINTER)

        with pytest.raises(ReconciliationHaltedError):
            brain.checkout("x")
        with pytest.raises(ReconciliationHaltedError):
            brain.join("x")
        assert brain.store.read_pointer(REFS_POINTER) == refs_before
        assert brain.current_branch() == "main"

    def test_an_interrupted_checkout_that_never_moved_the_head_is_undone(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        add(brain, "Nyquist")
        brain.checkout("main")
        table = brain._read_refs()
        assert table is not None
        brain._write_refs(table.model_copy(update={"switching": "x"}))  # Died before the head moved.

        reopened = Brain.open(tmp_path / "brain", actor=ANA)
        assert reopened.current_branch() == "main"
        assert labels(reopened) == {"Fourier"}

    def test_an_interrupted_checkout_that_moved_the_head_is_finished(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        add(brain, "Nyquist")
        brain.checkout("main")
        table = brain._read_refs()
        assert table is not None
        target = table.branches["x"]
        brain._write_refs(table.model_copy(update={"switching": "x"}))
        brain._write_head(target.snapshot, origin=None)  # Died after the head moved.

        reopened = Brain.open(tmp_path / "brain", actor=ANA)
        assert reopened.current_branch() == "x"
        assert labels(reopened) == {"Fourier", "Nyquist"}

    def test_deleting_the_current_branch_is_refused(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        with pytest.raises(BranchError, match="current branch"):
            brain.delete_branch("x")
        with pytest.raises(BranchNotFoundError):
            brain.delete_branch("nowhere")

    def test_deleting_the_only_name_for_work_needs_force(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        add(brain, "Nyquist")
        brain.checkout("main")

        with pytest.raises(UnmergedBranchError):
            brain.delete_branch("x")
        brain.delete_branch("x", force=True)
        assert [info.name for info in brain.branches()] == ["main"]

    def test_deleting_a_branch_another_contains_needs_no_force(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x")  # At main's head, so main contains it.
        add(brain, "Nyquist")
        brain.delete_branch("x")
        assert [info.name for info in brain.branches()] == ["main"]


class TestBranchHeadsAreRoots:
    """A named head is never reclaimed, however many commits land elsewhere (paper Section 7.5)."""

    def test_a_stale_branch_survives_a_prune(self, tmp_path: Path) -> None:
        policy = RetentionPolicy(retained_roots=2)
        brain = seeded(tmp_path / "brain", policy=policy)
        brain.create_branch("old", checkout=True)
        add(brain, "Nyquist")
        old_head = brain.snapshot().digest
        brain.checkout("main")
        for label in ("A", "B", "C", "D", "E"):
            add(brain, label)

        brain.prune(dry_run=False)

        brain.checkout("old")
        assert brain.snapshot().digest == old_head
        assert labels(brain) == {"Fourier", "Nyquist"}
        assert brain.verify()

    def test_branch_heads_stay_in_retained_outside_the_bound(self, tmp_path: Path) -> None:
        """What keeps a client that reads no refs from reclaiming them."""
        policy = RetentionPolicy(retained_roots=2)
        brain = seeded(tmp_path / "brain", policy=policy)
        brain.create_branch("old", checkout=True)
        add(brain, "Nyquist")
        old_head = brain.snapshot().digest
        brain.checkout("main")
        for label in ("A", "B", "C", "D", "E"):
            add(brain, label)

        assert brain._state is not None
        retained = brain._state.retained
        assert old_head in retained
        assert len([digest for digest in retained if digest != old_head]) <= 3  # Bound, plus main's own ref.

    def test_a_prune_keeps_a_head_a_refs_unaware_client_dropped_from_retained(self, tmp_path: Path) -> None:
        policy = RetentionPolicy(retained_roots=1)
        brain = seeded(tmp_path / "brain", policy=policy)
        brain.create_branch("old", checkout=True)
        add(brain, "Nyquist")
        old_head = brain.snapshot().digest
        brain.checkout("main")
        assert brain._state is not None
        # What an older SDK's commit leaves behind: the bound applied to every entry.
        brain._state = brain._state.model_copy(update={"retained": [brain._state.snapshot]})
        brain.store.write_pointer(
            "head", brain_module.canonicalize(brain._state.model_dump(mode="json", exclude_none=True))
        )

        brain.prune(dry_run=False)
        brain.checkout("old")
        assert brain.snapshot().digest == old_head
        assert brain.verify()


class TestJoin:
    """Joining a branch needs no new mechanism: a fast-forward, or the reconciliation that exists."""

    def test_a_branch_ahead_fast_forwards(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        add(brain, "Nyquist")
        ahead = brain.snapshot().digest
        brain.checkout("main")

        result = brain.join("x")
        assert result.outcome == "fast-forward"
        assert brain.snapshot().digest == ahead
        assert labels(brain) == {"Fourier", "Nyquist"}

    def test_a_contained_branch_is_already_joined(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x")
        add(brain, "Nyquist")
        head = brain.snapshot().digest
        assert brain.join("x").outcome == "up-to-date"
        assert brain.snapshot().digest == head

    def test_diverged_branches_need_a_strategy(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        add(brain, "Nyquist")
        brain.checkout("main")
        add(brain, "Laplace")

        with pytest.raises(BranchError, match="merge, rebase, or squash"):
            brain.join("x")
        with pytest.raises(BranchError, match="fast-forward"):
            brain.join("x", ReconcileStrategy.MERGE, fast_forward="only")

        result = brain.join("x", ReconcileStrategy.MERGE)
        assert result.outcome == "reconciled"
        assert result.reconciliation is not None
        assert labels(brain) == {"Fourier", "Nyquist", "Laplace"}
        assert set(brain.snapshot().parents) >= {brain.branches()[1].snapshot}

    def test_never_records_a_reconciliation_where_a_fast_forward_was_possible(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        brain.create_branch("x", checkout=True)
        add(brain, "Nyquist")
        theirs = brain.snapshot().digest
        brain.checkout("main")

        result = brain.join("x", ReconcileStrategy.MERGE, fast_forward="never")
        assert result.outcome == "reconciled"
        assert brain.snapshot().digest != theirs
        assert theirs in brain.snapshot().parents

    def test_joining_the_current_branch_is_refused(self, tmp_path: Path) -> None:
        brain = seeded(tmp_path / "brain")
        with pytest.raises(BranchError, match="current branch"):
            brain.join("main")
        with pytest.raises(BranchNotFoundError):
            brain.join("nowhere")


class TestPublishing:
    """Each branch publishes to its own tag, so writers on different branches never refuse each other."""

    async def test_two_writers_on_different_branches_never_diverge(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        ana = seeded(tmp_path / "ana")
        await ana.push(registry, REFERENCE, "latest")

        beto = Brain.open(tmp_path / "beto", actor=BETO)
        await beto.pull(registry, REFERENCE, "latest")
        beto.create_branch("beto/nyquist", checkout=True)
        add(beto, "Nyquist")
        await beto.push(registry)

        add(ana, "Laplace")
        await ana.push(registry)  # Would have been refused as divergence had both used one tag.

        assert set(registry.tags(REFERENCE)) == {"latest", "br.beto.nyquist"}
        assert await ana.remote_branches(registry) == {"main": "latest", "beto/nyquist": "br.beto.nyquist"}

    async def test_the_same_writers_on_one_tag_do_diverge(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The failure branches exist for, kept as a test so the contrast stays honest."""
        ana = seeded(tmp_path / "ana")
        await ana.push(registry, REFERENCE, "latest")
        beto = Brain.open(tmp_path / "beto", actor=BETO)
        await beto.pull(registry, REFERENCE, "latest")
        add(beto, "Nyquist")
        await beto.push(registry)

        add(ana, "Laplace")
        with pytest.raises(DivergenceError):
            await ana.push(registry)

    async def test_pulling_a_branch_tag_installs_into_that_branch(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        ana = seeded(tmp_path / "ana")
        await ana.push(registry, REFERENCE, "latest")
        ana.create_branch("ana/nyquist", checkout=True)
        add(ana, "Nyquist")
        await ana.push(registry)

        beto = Brain.open(tmp_path / "beto", actor=BETO)
        await beto.pull(registry, REFERENCE, "latest")
        add(beto, "Laplace")  # Unpublished work on main, which the pull must not overwrite.
        main_head = beto.snapshot().digest

        await beto.pull(registry, REFERENCE, "br.ana.nyquist")
        assert beto.current_branch() == "ana/nyquist"
        assert labels(beto) == {"Fourier", "Nyquist"}

        beto.checkout("main")
        assert beto.snapshot().digest == main_head
        assert labels(beto) == {"Fourier", "Laplace"}

    async def test_a_fresh_brain_pulling_a_branch_tag_starts_on_that_branch(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        ana = seeded(tmp_path / "ana")
        await ana.push(registry, REFERENCE, "latest")
        ana.create_branch("ana/nyquist", checkout=True)
        add(ana, "Nyquist")
        await ana.push(registry)

        beto = Brain.open(tmp_path / "beto", actor=BETO)
        await beto.pull(registry, REFERENCE, "br.ana.nyquist")
        assert beto.current_branch() == "ana/nyquist"
        add(beto, "Laplace")
        await beto.push(registry)  # Back to the branch's own tag, as a fast-forward.
        assert (await registry.resolve(REFERENCE, "br.ana.nyquist")).config.digest == beto.snapshot().digest

    async def test_an_explicit_tag_does_not_rename_the_branch(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        ana = seeded(tmp_path / "ana")
        await ana.push(registry, REFERENCE, "latest")
        ana.create_branch("x", checkout=True)
        await ana.push(registry, tag="v1")
        add(ana, "Nyquist")
        await ana.push(registry)

        assert set(registry.tags(REFERENCE)) == {"latest", "v1", "br.x"}
        assert next(info for info in ana.branches() if info.name == "x").tag == "br.x"

    async def test_pull_is_refused_while_a_reconciliation_is_open(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        ana = seeded(tmp_path / "ana")
        await ana.push(registry, REFERENCE, "latest")
        head = ana.snapshot().digest
        ana._put_reconcile_state(
            ReconcileState(
                theirs=head, ancestor=head, strategy=ReconcileStrategy.MERGE, actor=ANA, reason="r", head=head
            )
        )
        with pytest.raises(ReconciliationHaltedError):
            await ana.pull(registry, REFERENCE, "latest")

    async def test_the_local_registry_lists_tags(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        assert isinstance(registry, RegistryTags)
        await seeded(tmp_path / "ana").push(registry, REFERENCE, "latest")
        assert await registry.list_tags(REFERENCE) == ["latest"]


class Interloper(LocalLayoutRegistry):
    """A registry where another publisher's write lands right after this client's."""

    def __init__(self, root: Path, rival: Brain | None = None, stale: BrainManifest | None = None) -> None:
        super().__init__(root)
        self.rival = rival
        self.stale = stale
        self.stale_reads = 0

    async def push(self, reference: str, tag: str, manifest: BrainManifest, store: BlockStore) -> OciDigest:
        digest = await super().push(reference, tag, manifest, store)
        if self.rival is not None:
            rival, self.rival = self.rival, None
            await rival.push(LocalLayoutRegistry(self.root), reference, tag, force=True)
        return digest

    async def resolve(self, reference: str, tag: str) -> BrainManifest:
        if self.stale is not None and self.stale_reads > 0:
            self.stale_reads -= 1
            return self.stale
        return await super().resolve(reference, tag)


class TestLostPublish:
    """A publish another one replaced is reported, not silently assumed to have landed (paper Section 7.4)."""

    @pytest.fixture(autouse=True)
    def _no_waiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(brain_module, "PUBLISH_CONFIRM_DELAY", 0)

    async def test_a_publish_replaced_after_the_check_is_reported(self, tmp_path: Path) -> None:
        ana = seeded(tmp_path / "ana")
        plain = LocalLayoutRegistry(tmp_path / "registry")
        await ana.push(plain, REFERENCE, "latest")
        beto = Brain.open(tmp_path / "beto", actor=BETO)
        await beto.pull(plain, REFERENCE, "latest")
        add(beto, "Nyquist")
        add(ana, "Laplace")
        published = ana.snapshot().digest

        racing = Interloper(tmp_path / "registry", rival=beto)
        with pytest.raises(LostPublishError) as caught:
            await ana.push(racing)

        assert caught.value.published == str(published)
        assert caught.value.observed == str(beto.snapshot().digest)
        assert ana.snapshot().digest == published  # Nothing lost locally.

    async def test_a_registry_still_serving_an_ancestor_is_retried(self, tmp_path: Path) -> None:
        ana = seeded(tmp_path / "ana")
        plain = LocalLayoutRegistry(tmp_path / "registry")
        await ana.push(plain, REFERENCE, "latest")
        lagging = Interloper(tmp_path / "registry", stale=await plain.resolve(REFERENCE, "latest"))
        add(ana, "Laplace")

        lagging.stale_reads = 1
        await ana.push(lagging)  # One stale read, then the write is served.
        assert (await plain.resolve(REFERENCE, "latest")).config.digest == ana.snapshot().digest

    async def test_a_registry_that_never_serves_the_write_is_unconfirmed_not_lost(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        ana = seeded(tmp_path / "ana")
        plain = LocalLayoutRegistry(tmp_path / "registry")
        await ana.push(plain, REFERENCE, "latest")
        lagging = Interloper(tmp_path / "registry", stale=await plain.resolve(REFERENCE, "latest"))
        add(ana, "Laplace")

        lagging.stale_reads = 99
        with caplog.at_level(logging.WARNING):
            await ana.push(lagging)
        assert "PUBLISH_UNCONFIRMED" in caplog.text
