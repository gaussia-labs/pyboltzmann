"""Divergence and reconciliation, end to end (paper Section 12).

Detecting divergence and refusing to overwrite is only half of the obligation. These tests exercise the
other half: two brains that advanced from a common ancestor are reconciled into a snapshot naming both
histories, an external contribution is judged before it is incorporated, and a partial install is
published back rather than refused.

The transport is :class:`LocalLayoutRegistry`, for the same reason the distribution tests use it: an OCI
layout is a first-class transport target and it exercises the code path a network registry would.
"""

from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.distribution.local import LocalLayoutRegistry
from boltzmann.exceptions import (
    DivergenceError,
    MultipleMergeBasesError,
    NoCommonAncestorError,
    ReconciliationBlockedError,
    ReconciliationError,
    ReconciliationHaltedError,
    ResolutionRefusedError,
)
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.ingest.validation import ValidationStatus
from boltzmann.ingest.validators import UndecidedValidator
from boltzmann.module.ledger import Ledger
from boltzmann.module.module import Module
from boltzmann.module.snapshot import Snapshot
from boltzmann.reconcile import MissingEvidence, ReconcileRequest, ReconcileStrategy
from boltzmann.reconcile.ancestry import common_ancestor, composition_at, snapshot_at
from boltzmann.reconcile.gate import RECONCILE_VALIDATORS
from boltzmann.reconcile.resolution import ResolutionKind
from boltzmann.retention.policy import PERMISSIVE_POLICY
from boltzmann.retention.requests import DropRequest

MAINTAINER = Actor(id="maintainer@example.org", kind=ActorKind.HUMAN)
CONTRIBUTOR = Actor(id="contributor@example.org", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
UPSTREAM = "registry.example/org/brain"
PROPOSAL = "registry.example/sam/brain"


def llm(*labels: str):
    """A proposer that derives one semantic block per label from whatever it is handed."""

    def propose(task, source: bytes) -> CandidateSet:
        return CandidateSet(
            producer=MODEL,
            candidates=[
                Candidate(
                    memory_type=MemoryType.SEMANTIC,
                    evidence=[task.source],
                    payload={"kind": "formula", "label": label, "statement": f"about {label}"},
                )
                for label in labels
            ],
        )

    return propose


def claim(label: str, statement: str):
    """A proposer that states one claim, so two of them about the same thing can disagree."""

    def propose(task, source: bytes) -> CandidateSet:
        return CandidateSet(
            producer=MODEL,
            candidates=[
                Candidate(
                    memory_type=MemoryType.SEMANTIC,
                    evidence=[task.source],
                    payload={"kind": "fact", "label": label, "statement": statement, "subject": "signals"},
                )
            ],
        )

    return propose


@pytest.fixture
def registry(tmp_path: Path) -> LocalLayoutRegistry:
    return LocalLayoutRegistry(tmp_path / "registry")


def paper(actor: Actor = MAINTAINER) -> RegistrationRequest:
    return RegistrationRequest(media_type="application/pdf", actor=actor)


async def published(path: Path, registry: LocalLayoutRegistry) -> Brain:
    """A brain with one source ingested, published at ``UPSTREAM:v1``."""
    brain = Brain.open(path, actor=MAINTAINER, policy=PERMISSIVE_POLICY)
    brain.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("Fourier"))
    await brain.push(registry, UPSTREAM, "v1")
    return brain


async def contributed(path: Path, registry: LocalLayoutRegistry, label: str = "Laplace") -> Brain:
    """A brain installed from ``UPSTREAM:v1``, extended, and published to its own repository."""
    brain = Brain.open(path, actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
    await brain.pull(registry, UPSTREAM, "v1")
    brain.ingest(b"%PDF-1.7 Lecture 08", paper(CONTRIBUTOR), llm(label))
    await brain.push(registry, PROPOSAL, "proposal")
    return brain


class TestDivergence:
    """The remote is not an ancestor, so publishing would drop work."""

    async def test_a_diverged_push_is_refused_distinguishably(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """``DivergenceError`` rather than a bare distribution failure: this is the one refusal with a
        defined remedy, and a caller that cannot tell it apart cannot offer to reconcile."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        upstream.ingest(b"%PDF-1.7 Lecture 10", paper(), llm("Hankel"))

        with pytest.raises(DivergenceError, match="diverged"):
            await upstream.push(registry, PROPOSAL, "proposal")

    async def test_reconciling_makes_the_push_a_fast_forward(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """After a merge the other history is a parent, so it is contained and publishing drops nothing.

        Walking only the first-parent chain would answer this wrongly in exactly the case reconciliation
        exists for.
        """
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        upstream.ingest(b"%PDF-1.7 Lecture 10", paper(), llm("Hankel"))

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        upstream.merge(fetched.digest, reason="reviewed")

        await upstream.push(registry, PROPOSAL, "proposal")  # no force

    async def test_unrelated_histories_are_refused_rather_than_guessed(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Without an ancestor, a block on one side and not the other is ambiguous between "they added
        it" and "I dropped it", and those demand opposite outcomes."""
        upstream = await published(tmp_path / "upstream", registry)

        stranger = Brain.open(tmp_path / "stranger", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        stranger.ingest(b"%PDF-1.7 Unrelated", paper(CONTRIBUTOR), llm("Chebyshev"))
        await stranger.push(registry, "registry.example/other/brain", "v1")

        fetched = await upstream.fetch(registry, "registry.example/other/brain", "v1")
        with pytest.raises(NoCommonAncestorError, match="share no ancestor"):
            upstream.plan_reconcile(fetched.digest)


class TestBestCommonAncestor:
    """The merge base is unique by ancestry, not by traversal order or distance."""

    def test_criss_cross_history_is_refused_even_with_an_older_hint(self) -> None:
        from boltzmann.store.memory import MemoryBlockStore

        store = MemoryBlockStore()

        def keep(snapshot: Snapshot) -> OciDigest:
            assert store.put_bytes(snapshot.canonical_bytes()) == snapshot.digest
            return snapshot.digest

        root = Snapshot(created_at="2026-08-28T10:00:00Z")
        keep(root)
        left = Snapshot(parents=[root.digest], created_at="2026-08-28T10:01:00Z")
        right = Snapshot(parents=[root.digest], created_at="2026-08-28T10:02:00Z")
        keep(left)
        keep(right)
        ours = Snapshot(parents=[left.digest, right.digest], created_at="2026-08-28T10:03:00Z")
        theirs = Snapshot(parents=[right.digest, left.digest], created_at="2026-08-28T10:04:00Z")
        keep(ours)
        keep(theirs)

        with pytest.raises(MultipleMergeBasesError) as raised:
            common_ancestor(
                store,
                {ours.digest, left.digest, right.digest, root.digest},
                theirs,
                theirs.digest,
                hint=root.digest,
            )

        assert str(left.digest) in str(raised.value)
        assert str(right.digest) in str(raised.value)


class TestFetch:
    """Retrieving a history is not adopting it."""

    async def test_fetch_leaves_the_pointer_where_it_was(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The step at which nothing has changed yet: two histories are held locally while the published
        brain is untouched."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)

        before = upstream.snapshot().digest
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")

        assert upstream.snapshot().digest == before
        assert fetched.snapshot.digest != before
        assert fetched.block_count > 0

    async def test_fetch_transfers_only_the_delta(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The contributor's brain shares every block it did not change, byte for byte, so a contribution
        of forty blocks is forty blocks and not a brain."""
        upstream = await published(tmp_path / "upstream", registry)
        contributor = await contributed(tmp_path / "contrib", registry)

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")

        assert fetched.block_count < contributor.snapshot().block_count
        assert set(fetched.incoming) <= {MemoryType.CANONICAL, MemoryType.SEMANTIC, MemoryType.PROVENANCE}

    async def test_the_fetched_history_is_walkable(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """A snapshot names its parents, so an artifact that published only its head would hand over a
        lineage whose links resolve to nothing."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")

        parent = fetched.snapshot.first_parent
        assert parent is not None
        assert upstream.store.is_resolvable(parent)


class TestPlan:
    """The contribution is judged mechanically, before any decision."""

    async def test_the_plan_reports_the_ancestor_and_every_verdict(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Reviewing a pull request means reading a diff; here every incoming block arrives with a
        verdict, so which parts fit is known before anything is decided."""
        upstream = await published(tmp_path / "upstream", registry)
        published_at = upstream.snapshot().digest
        await contributed(tmp_path / "contrib", registry)
        upstream.ingest(b"%PDF-1.7 Lecture 10", paper(), llm("Hankel"))

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        plan = upstream.plan_reconcile(fetched.digest)

        assert plan.ancestor == published_at
        assert plan.incoming.verdicts
        assert plan.incoming.is_clean
        assert not plan.is_blocked
        assert not plan.is_noop

    async def test_the_plan_prices_all_three_strategies(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The composition is identical under all three, so the plan does not ask which one was chosen --
        its job is to inform that choice."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        plan = upstream.plan_reconcile(fetched.digest)

        assert set(plan.attribution) == set(ReconcileStrategy)
        assert plan.attribution[ReconcileStrategy.MERGE].their_signatures_survive
        assert not plan.attribution[ReconcileStrategy.REBASE].their_signatures_survive
        assert not plan.attribution[ReconcileStrategy.SQUASH].their_signatures_survive
        assert plan.collapsed > 0

    async def test_a_history_already_contained_is_a_noop(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Reconciling with something this brain already holds writes nothing, the way an up-to-date merge
        does in version control."""
        upstream = await published(tmp_path / "upstream", registry)
        before = upstream.snapshot().digest

        result = upstream.merge(before, reason="already here")

        assert result.snapshots == []
        assert upstream.snapshot().digest == before


class TestWhoOfferedIt:
    """A contribution names its author, and being unlisted is not being an attacker."""

    async def test_an_unsigned_contribution_reports_no_authorship(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Nothing was claimed, so nothing is reported -- the zero-configuration case, unchanged."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")

        plan = upstream.plan_reconcile(fetched.digest)

        assert plan.authorship is None


class TestStrategies:
    """All three land the same blocks. What differs is who stays on record."""

    @pytest.mark.parametrize("strategy", list(ReconcileStrategy))
    async def test_every_strategy_lands_the_same_composition(
        self, tmp_path: Path, registry: LocalLayoutRegistry, strategy: ReconcileStrategy
    ) -> None:
        """This is the claim of Table 3, and the reason choosing between them is a question of attribution
        rather than of outcome."""
        upstream = await published(tmp_path / f"upstream-{strategy.value}", registry)
        await contributed(tmp_path / f"contrib-{strategy.value}", registry)
        upstream.ingest(b"%PDF-1.7 Lecture 10", paper(), llm("Hankel"))

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        result = upstream.reconcile(
            ReconcileRequest(
                theirs=fetched.digest,
                strategy=strategy,
                actor=MAINTAINER,
                reason="reviewed contribution",
            )
        )

        assert result.strategy is strategy
        assert upstream.verify()
        assert {kind: set(ids) for kind, ids in result.admitted.items()} == {
            kind: set(upstream.module(kind).block_ids) for kind in result.admitted
        }

    async def test_only_merge_records_the_other_history_as_a_parent(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")

        merged = upstream.merge(fetched.digest, reason="reviewed")

        assert fetched.digest in merged.parents
        assert merged.snapshot.is_reconciliation
        assert merged.snapshot.first_parent not in (fetched.digest,)

    async def test_rebase_writes_one_snapshot_per_replayed_version(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Replaying immutable blocks is deterministic and it mints new identities, which is what
        invalidates any signature over the originals."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        plan = upstream.plan_reconcile(fetched.digest)

        result = upstream.rebase(fetched.digest, reason="tidy contribution")

        assert len(result.snapshots) == plan.replayable
        assert fetched.digest not in result.parents
        assert result.attribution.mints_new_identities

    def test_rebase_preserves_granularity_where_the_versions_can_be_reopened(self, tmp_path: Path) -> None:
        """Two histories over one layout: every version is reopenable, so a rebase restates each of them
        rather than folding the run into a single snapshot.

        The fetched case cannot do this, and says so -- ``plan.replayable`` below ``plan.collapsed`` means the
        artifact carried only its head's compositions, because a Merkle root commits to a set without being
        invertible into it.
        """
        theirs = Brain.open(tmp_path / "brain", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        theirs.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("Fourier"))

        # A second handle on the same layout: a separate history whose versions this store still holds.
        ours = Brain.open(tmp_path / "brain", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        ours.ingest(b"%PDF-1.7 Lecture 08", paper(CONTRIBUTOR), llm("Laplace"))
        ours.ingest(b"%PDF-1.7 Lecture 09", paper(CONTRIBUTOR), llm("Bessel"))
        contributed_head = ours.snapshot().digest

        theirs.ingest(b"%PDF-1.7 Lecture 10", paper(), llm("Hankel"))
        plan = theirs.plan_reconcile(contributed_head)
        assert plan.replayable == plan.collapsed > 1

        result = theirs.rebase(contributed_head, reason="replay their versions onto mine")

        assert len(result.snapshots) == plan.collapsed
        assert result.attribution.snapshots_written == plan.collapsed
        assert theirs.verify()

    async def test_squash_collapses_the_chain_into_one_snapshot(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")

        result = upstream.squash(fetched.digest, reason="one version is enough")

        assert len(result.snapshots) == 1
        assert fetched.digest not in result.parents

    async def test_squash_preserves_every_provenance_record_it_collapsed(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """It compacts snapshot history, not the audit ledger. Provenance is a module, so Equation 1 keeps
        every record either side produced -- destroying the ledger to tidy a chain is not expressible."""
        upstream = await published(tmp_path / "upstream", registry)
        contributor = await contributed(tmp_path / "contrib", registry)
        theirs = set(contributor.module(MemoryType.PROVENANCE).block_ids)

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        upstream.squash(fetched.digest, reason="one version is enough")

        assert theirs <= set(upstream.module(MemoryType.PROVENANCE).block_ids)

    async def test_the_strategy_has_no_default(self) -> None:
        """The three differ in attribution, so a default would be this SDK choosing whose name comes off
        the work."""
        with pytest.raises(ValueError, match="strategy"):
            ReconcileRequest(theirs=OciDigest.of(b"x"), actor=MAINTAINER, reason="why")  # type: ignore[call-arg]


class TestSemanticConflicts:
    """A conflict here is a validation failure, not a differencing failure."""

    async def test_a_drop_on_one_side_rejects_a_derivation_on_the_other(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The case that proves reconciliation cannot be purely structural. Each module's set arithmetic is
        individually correct and the result still violates R1, because the invariant that broke lives
        between modules."""
        upstream = await published(tmp_path / "upstream", registry)
        contributor = await contributed(tmp_path / "contrib", registry)

        # The contributor derives from evidence the maintainer is about to judge wrong.
        shared = next(iter(upstream.module(MemoryType.CANONICAL).block_ids))
        upstream.drop(
            DropRequest(
                blocks=[shared],
                memory_type=MemoryType.CANONICAL,
                actor=MAINTAINER,
                reason="the lecture was withdrawn",
            )
        )
        contributor.ingest(b"%PDF-1.7 Lecture 07", paper(CONTRIBUTOR), llm("Fourier restated"))
        await contributor.push(registry, PROPOSAL, "proposal", force=True)

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        plan = upstream.plan_reconcile(fetched.digest)

        assert shared not in plan.admitted(MemoryType.CANONICAL)  # removal wins
        # Auditable without a new record: the history that dropped it wrote a removal record, and that
        # record is a provenance block, which Equation 1 folds in along with everything else.
        assert shared in plan.excluded[MemoryType.CANONICAL]

        rejected = plan.incoming.by_status(ValidationStatus.REJECTED)
        assert rejected
        assert any("evidence" in issue.field for verdict in rejected for issue in verdict.issues if issue.field)

    async def test_deliberate_removal_and_never_held_are_told_apart(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Same verdict, opposite advice: one contributor should not resend, the other should resend whole.
        Collapsing them discards legitimate work over a packaging mistake."""
        upstream = await published(tmp_path / "upstream", registry)
        contributor = await contributed(tmp_path / "contrib", registry)

        dropped = next(iter(upstream.module(MemoryType.CANONICAL).block_ids))
        upstream.drop(
            DropRequest(
                blocks=[dropped],
                memory_type=MemoryType.CANONICAL,
                actor=MAINTAINER,
                reason="the lecture was withdrawn",
            )
        )
        contributor.ingest(b"%PDF-1.7 Lecture 07", paper(CONTRIBUTOR), llm("Fourier restated"))
        await contributor.push(registry, PROPOSAL, "proposal", force=True)

        # Only the derived modules are fetched, so the contributor's own new source never arrives.
        fetched = await upstream.fetch(
            registry, PROPOSAL, "proposal", modules=[MemoryType.SEMANTIC, MemoryType.PROVENANCE]
        )
        plan = upstream.plan_reconcile(fetched.digest)

        assert plan.incoming.advice[dropped] is MissingEvidence.DROPPED_DELIBERATELY
        assert MissingEvidence.NEVER_HELD in plan.incoming.advice.values()
        # And the incompleteness is visible on its own, rather than having to be inferred from a rejection.
        assert MemoryType.CANONICAL in plan.untransferred

    async def test_a_check_that_declines_to_decide_stops_the_commit(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The protocol declined to decide, and committing would decide for it."""
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        before = upstream.snapshot().digest

        checks = [*RECONCILE_VALIDATORS, UndecidedValidator()]
        with pytest.raises(ReconciliationHaltedError, match="need a decision"):
            upstream.reconcile(
                ReconcileRequest(
                    theirs=fetched.digest,
                    strategy=ReconcileStrategy.MERGE,
                    actor=MAINTAINER,
                    reason="reviewed",
                ),
                validators=checks,
            )

        assert upstream.snapshot().digest == before
        status = upstream.reconcile_status(validators=checks)
        assert status is not None
        assert status.unresolved


class TestPartialPublish:
    """A partial install is published back as a reconciliation rather than refused."""

    async def test_a_partial_install_can_be_published_back(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The modules the publisher does not hold take their roots from the remote unchanged, so no module
        regresses and nothing the artifact named disappears."""
        upstream = await published(tmp_path / "upstream", registry)
        upstream.ingest(b"%PDF-1.7 Lecture 10", paper(), llm("Hankel"))
        await upstream.push(registry, UPSTREAM, "v1")

        partial = Brain.open(tmp_path / "partial", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        await partial.pull(registry, UPSTREAM, "v1", modules=[MemoryType.CANONICAL, MemoryType.SEMANTIC])
        assert partial.origin is not None
        assert partial.origin.partial

        fetched = await partial.fetch(registry, UPSTREAM, "v1")
        partial.merge(fetched.digest, reason="publishing back what I hold")

        assert partial.snapshot().has_module(MemoryType.PROVENANCE)
        await partial.push(registry, UPSTREAM, "v1")

        manifest = await registry.resolve(UPSTREAM, "v1")
        assert manifest.modules == upstream.snapshot().installed

    async def test_a_module_that_never_arrived_is_carried_at_the_remotes_root(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Section 12.8 read literally: the modules the publisher does not hold take their roots from the
        remote unchanged. A root is a complete statement of a version, so adopting one does not require
        holding what it commits to -- and without this, publishing back would uninstall them."""
        await published(tmp_path / "upstream", registry)

        partial = Brain.open(tmp_path / "partial", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        await partial.pull(registry, UPSTREAM, "v1", modules=[MemoryType.SEMANTIC])

        # Only the module it holds is retrieved, so the others are named by the remote and openable nowhere.
        fetched = await partial.fetch(registry, UPSTREAM, "v1", modules=[MemoryType.SEMANTIC])
        plan = partial.plan_reconcile(fetched.digest)

        assert MemoryType.PROVENANCE in plan.carried
        assert MemoryType.CANONICAL in plan.carried

        partial.merge(fetched.digest, reason="publishing back what I hold")
        remote = await registry.resolve(UPSTREAM, "v1")

        for memory_type, reference in plan.carried.items():
            assert partial.snapshot().modules[memory_type].root == reference.root
        assert partial.snapshot().installed == remote.modules


class TestResolvingByHand:
    """What did not apply cleanly waits for a person, the way a merge conflict does.

    The difference from version control is what a person is allowed to decide. There, anything can be forced
    into a commit, because what was merged is text and the consequences are a human's to judge. Here some
    invariants are structural, so the decisions on offer are the ones that settle a conflict rather than
    conceal one.
    """

    async def contradicting(self, tmp_path: Path, registry: LocalLayoutRegistry) -> tuple[Brain, OciDigest]:
        """An upstream and a fetched contribution that each added a claim disagreeing with the other's.

        Both sides have to add it *after* they parted, which is the only way this arises: a claim that
        contradicts something already held is caught by the ingestion gate at the moment it is proposed, so it
        never gets published. What reconciliation meets is two claims that were each admissible where they
        were made -- the case Section 12.4 lists as not structurally detectable at all.
        """
        seed = await published(tmp_path / "seed", registry)
        assert seed.snapshot().block_count  # the shared ancestor states nothing about Fourier's convergence

        upstream = Brain.open(tmp_path / "upstream", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        await upstream.pull(registry, UPSTREAM, "v1")
        upstream.ingest(b"%PDF-1.7 Lecture 08", paper(), claim("convergence", "converges pointwise"))

        contributor = Brain.open(tmp_path / "contrib", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        await contributor.pull(registry, UPSTREAM, "v1")
        contributor.ingest(b"%PDF-1.7 Lecture 09", paper(CONTRIBUTOR), claim("convergence", "diverges everywhere"))
        await contributor.push(registry, PROPOSAL, "proposal")

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        return upstream, fetched.digest

    async def test_nothing_is_written_when_something_does_not_apply(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Committing the part that fits would be a decision about the rest, taken without asking."""
        upstream, theirs = await self.contradicting(tmp_path, registry)
        before = upstream.snapshot().digest
        held = set(upstream.module(MemoryType.SEMANTIC).block_ids)

        with pytest.raises(ReconciliationHaltedError, match="Nothing was written"):
            upstream.merge(theirs, reason="reviewed")

        assert upstream.snapshot().digest == before
        assert set(upstream.module(MemoryType.SEMANTIC).block_ids) == held

    async def test_the_state_outlives_the_process(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """A second pointer next to the head, like the MERGE_HEAD of version control: a decision taken today
        is still there tomorrow, and a separate tool can read what is open."""
        upstream, theirs = await self.contradicting(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        reopened = Brain.open(tmp_path / "upstream", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        status = reopened.reconcile_status()

        assert status is not None
        assert status.state.theirs == theirs
        assert status.state.strategy is ReconcileStrategy.MERGE
        assert len(status.unresolved) == 1

    async def test_an_ordinary_write_is_refused_while_one_is_open(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The decisions were taken against a particular head, so a commit underneath them would leave them
        describing a reconciliation that no longer exists."""
        upstream, theirs = await self.contradicting(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        with pytest.raises(ReconciliationHaltedError, match="cannot write to this brain"):
            upstream.ingest(b"%PDF-1.7 Lecture 09", paper(), claim("Bessel", "is a function"))

    async def test_a_second_reconciliation_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        upstream, theirs = await self.contradicting(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        with pytest.raises(ReconciliationHaltedError, match="still unresolved"):
            upstream.merge(theirs, reason="again")

    async def test_continuing_early_names_what_is_open(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        upstream, theirs = await self.contradicting(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        with pytest.raises(ReconciliationBlockedError, match="still open"):
            upstream.reconcile_continue()

    async def test_rejecting_leaves_it_out_and_commits_the_rest(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """The block is not destroyed. It stays in the store and in the history it came from; the new root
        simply does not name it."""
        upstream, theirs = await self.contradicting(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        opened = upstream.reconcile_status()
        assert opened is not None
        contested = opened.unresolved[0]
        status = upstream.reconcile_resolve(contested, ResolutionKind.REJECT, reason="ours is right")
        assert status.is_resolved

        result = upstream.reconcile_continue()

        assert contested not in upstream.module(MemoryType.SEMANTIC).block_ids
        assert upstream.store.is_resolvable(contested)  # held, just not named
        assert result.snapshot.is_reconciliation
        assert upstream.verify()

    async def test_admitting_a_contradiction_is_allowed(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """A contradiction is information, not a defect: holding two claims that disagree is a legitimate
        state, and which one is right is not a question the protocol answers."""
        upstream, theirs = await self.contradicting(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        status = upstream.reconcile_status()
        assert status is not None
        contested = status.unresolved[0]
        assert status.verdict_for(contested) is ValidationStatus.CONTRADICTED

        upstream.reconcile_resolve(contested, ResolutionKind.ADMIT, reason="both readings are on record")
        upstream.reconcile_continue()

        assert contested in upstream.module(MemoryType.SEMANTIC).block_ids
        assert upstream.verify()

    async def test_admitting_a_rejection_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The one place this departs from version control on purpose. A derived block whose evidence the
        composition does not hold cannot be audited against its source, and no later check would notice --
        ``verify`` recomputes hashes and compositions, not citations across modules."""
        upstream = await published(tmp_path / "upstream", registry)
        contributor = await contributed(tmp_path / "contrib", registry)

        dropped = next(iter(upstream.module(MemoryType.CANONICAL).block_ids))
        upstream.drop(
            DropRequest(
                blocks=[dropped],
                memory_type=MemoryType.CANONICAL,
                actor=MAINTAINER,
                reason="the lecture was withdrawn",
            )
        )
        contributor.ingest(b"%PDF-1.7 Lecture 07", paper(CONTRIBUTOR), llm("Fourier restated"))
        await contributor.push(registry, PROPOSAL, "proposal", force=True)

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(fetched.digest, reason="reviewed")

        status = upstream.reconcile_status()
        assert status is not None
        rejected = next(block for block in status.unresolved if status.verdict_for(block) is ValidationStatus.REJECTED)

        with pytest.raises(ResolutionRefusedError, match="cannot be admitted by decision"):
            upstream.reconcile_resolve(rejected, ResolutionKind.ADMIT, reason="I want it anyway")
        # And the message points at the operation that fixes the cause, not at a way around it.
        with pytest.raises(ResolutionRefusedError, match="re-admit the evidence"):
            upstream.reconcile_resolve(rejected, ResolutionKind.ADMIT)

    async def test_abandoning_leaves_the_brain_untouched(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Nothing is undone because nothing was written."""
        upstream, theirs = await self.contradicting(tmp_path, registry)
        before = upstream.snapshot().digest
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        upstream.reconcile_abort()

        assert upstream.reconcile_status() is None
        assert upstream.snapshot().digest == before
        upstream.ingest(b"%PDF-1.7 Lecture 09", paper(), claim("Bessel", "is a function"))  # writing works again

    async def test_nothing_to_resolve_is_not_an_error_state(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=MAINTAINER, policy=PERMISSIVE_POLICY)

        assert brain.reconcile_status() is None
        with pytest.raises(ReconciliationError, match="nothing to abandon"):
            brain.reconcile_abort()
        with pytest.raises(ReconciliationError, match="nothing to continue"):
            brain.reconcile_continue()


class TestPrecedence:
    """Two histories replaced the same block with different successors."""

    async def competing(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> tuple[Brain, OciDigest, dict[str, BlockId]]:
        """An upstream and a contribution that supersede one block with two different successors."""
        seed = Brain.open(tmp_path / "seed", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        seed.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("original", "successor-a", "successor-b"))
        await seed.push(registry, UPSTREAM, "v1")
        semantic = seed.module(MemoryType.SEMANTIC)
        ids = {semantic.get(block).label: block for block in semantic.block_ids}

        upstream = Brain.open(tmp_path / "upstream", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        await upstream.pull(registry, UPSTREAM, "v1")
        contributor = Brain.open(tmp_path / "contrib", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        await contributor.pull(registry, UPSTREAM, "v1")

        upstream.supersede(ids["successor-a"], ids["original"], MemoryType.SEMANTIC, reason="a supersedes it")
        contributor.supersede(ids["successor-b"], ids["original"], MemoryType.SEMANTIC, reason="b supersedes it")
        await contributor.push(registry, PROPOSAL, "proposal")

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        return upstream, fetched.digest, ids

    async def test_it_is_not_resolved_silently(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """Both successors are admissible and both edges are recorded; which one takes precedence is not the
        protocol's to decide, and a ledger that kept only its last reader's answer would decide it."""
        upstream, theirs, ids = await self.competing(tmp_path, registry)

        plan = upstream.plan_reconcile(theirs)

        assert plan.is_blocked
        pending = plan.incoming.by_status(ValidationStatus.PENDING_REVIEW)
        assert len(pending) == 1
        assert set(pending[0].conflicts_with) == {ids["successor-a"], ids["successor-b"]}

    async def test_preferring_one_settles_it_without_erasing_the_other(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Precedence is recorded the only way this architecture can record it: one more supersession edge,
        from the winner over the loser. Both original edges stay, because the record of what happened is not
        what gets resolved."""
        upstream, theirs, ids = await self.competing(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        status = upstream.reconcile_status()
        assert status is not None
        upstream.reconcile_resolve(
            status.unresolved[0],
            ResolutionKind.PREFER,
            prefer=ids["successor-b"],
            reason="b is the edition we keep",
        )
        upstream.reconcile_continue()

        ledger = Ledger.of(upstream.modules())
        assert ledger.successors_of(ids["original"]) == {ids["successor-a"], ids["successor-b"]}
        assert ledger.contested(ids["original"]) == set()
        assert not ledger.is_accessible(ids["successor-a"])
        assert ledger.is_accessible(ids["successor-b"])
        assert upstream.verify()

    async def test_preferring_something_that_was_never_on_offer_is_refused(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        upstream, theirs, ids = await self.competing(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        opened = upstream.reconcile_status()
        assert opened is not None
        with pytest.raises(ResolutionRefusedError, match="must name one of the competing successors"):
            upstream.reconcile_resolve(opened.unresolved[0], ResolutionKind.PREFER, prefer=ids["original"])


class TestRemovals:
    """A reconciliation can take work out of this brain, and that is a decision someone has to make."""

    async def dropped_by_them(self, tmp_path: Path, registry: LocalLayoutRegistry) -> tuple[Brain, OciDigest, BlockId]:
        """An upstream, and a contribution whose history withdrew a block the upstream still holds."""
        seed = Brain.open(tmp_path / "seed", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        seed.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("kept", "withdrawn-later"))
        await seed.push(registry, UPSTREAM, "v1")
        semantic = seed.module(MemoryType.SEMANTIC)
        doomed = next(block for block in semantic.block_ids if semantic.get(block).label == "withdrawn-later")

        upstream = Brain.open(tmp_path / "upstream", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        await upstream.pull(registry, UPSTREAM, "v1")
        contributor = Brain.open(tmp_path / "contrib", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        await contributor.pull(registry, UPSTREAM, "v1")

        contributor.drop(
            DropRequest(
                blocks=[doomed],
                memory_type=MemoryType.SEMANTIC,
                actor=CONTRIBUTOR,
                reason="the claim was wrong",
            )
        )
        await contributor.push(registry, PROPOSAL, "proposal")

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        return upstream, fetched.digest, doomed

    async def test_it_stops_rather_than_removing_work_unasked(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Exclusion has precedence in Equation 1, so their drop does take effect here -- which is exactly
        why it cannot happen without being seen. Applying it silently is a decision taken on the operator's
        behalf, the same thing the halt exists to prevent for an incoming block."""
        upstream, theirs, doomed = await self.dropped_by_them(tmp_path, registry)
        before = upstream.snapshot().digest

        with pytest.raises(ReconciliationHaltedError, match="would be removed"):
            upstream.merge(theirs, reason="reviewed")

        assert upstream.snapshot().digest == before
        assert doomed in upstream.module(MemoryType.SEMANTIC).block_ids

        status = upstream.reconcile_status()
        assert status is not None
        assert status.withdrawn[MemoryType.SEMANTIC] == [doomed]
        assert not status.removals_accepted
        assert not status.is_resolved

    async def test_continuing_without_accepting_is_refused(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        upstream, theirs, _ = await self.dropped_by_them(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        with pytest.raises(ReconciliationBlockedError, match="nothing has said that is acceptable"):
            upstream.reconcile_continue()

    async def test_accepting_lets_the_removal_through(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The block is not destroyed: older retained roots still name it and still verify, and the bytes
        stay in the store. Only the new root does not name it."""
        upstream, theirs, doomed = await self.dropped_by_them(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="reviewed")

        status = upstream.reconcile_accept_removals(reason="they are right, the claim was wrong")
        assert status.is_resolved

        upstream.reconcile_continue()

        assert doomed not in upstream.module(MemoryType.SEMANTIC).block_ids
        assert upstream.store.is_resolvable(doomed)
        # Auditable without a record written here: the history that dropped it wrote one, and Equation 1
        # keeps it like any other provenance block.
        assert doomed in Ledger.of(upstream.modules()).removed
        assert upstream.verify()

    async def test_accepting_nothing_is_an_error(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        upstream = await published(tmp_path / "upstream", registry)
        await contributed(tmp_path / "contrib", registry)
        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        upstream.merge(fetched.digest, reason="clean contribution")

        with pytest.raises(ReconciliationError, match="nothing to accept"):
            upstream.reconcile_accept_removals()


class TestCascade:
    """Withdrawing evidence takes what cited it, whichever history withdrew it."""

    async def orphaning(self, tmp_path: Path, registry: LocalLayoutRegistry) -> tuple[Brain, OciDigest, BlockId]:
        """An upstream holding a block derived from a source the other history then withdrew.

        The mirror of the case Section 12.4 walks through. There the derived block is the incoming one and
        the validation gate rejects it; here it is already held, so nobody proposed it and no gate looks at
        it -- which is why the cascade has to.
        """
        seed = Brain.open(tmp_path / "seed", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        seed.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("first"))
        await seed.push(registry, UPSTREAM, "v1")
        source = next(iter(seed.module(MemoryType.CANONICAL).block_ids))

        upstream = Brain.open(tmp_path / "upstream", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        await upstream.pull(registry, UPSTREAM, "v1")
        contributor = Brain.open(tmp_path / "contrib", actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        await contributor.pull(registry, UPSTREAM, "v1")

        # Derived here, after the two parted, from evidence they both still hold.
        task = upstream.define_task(source, allowed=[MemoryType.SEMANTIC])
        upstream.commit(
            upstream.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.SEMANTIC,
                            evidence=[source],
                            payload={
                                "kind": "fact",
                                "label": "a later reading",
                                "statement": "derived from the source",
                                "subject": "signals",
                            },
                        )
                    ],
                ),
                task,
            )
        )
        semantic = upstream.module(MemoryType.SEMANTIC)
        derived = next(block for block in semantic.block_ids if semantic.get(block).label == "a later reading")

        contributor.drop(
            DropRequest(
                blocks=[source],
                memory_type=MemoryType.CANONICAL,
                actor=CONTRIBUTOR,
                reason="the lecture was withdrawn",
            )
        )
        await contributor.push(registry, PROPOSAL, "proposal")

        fetched = await upstream.fetch(registry, PROPOSAL, "proposal")
        return upstream, fetched.digest, derived

    async def test_a_withdrawn_source_takes_the_blocks_that_cite_it(
        self, tmp_path: Path, registry: LocalLayoutRegistry
    ) -> None:
        """Equation 1 is correct per module and the invariant it breaks runs between them: the canonical
        block leaves and its dependent stays behind, citing evidence the composition no longer holds."""
        upstream, theirs, derived = await self.orphaning(tmp_path, registry)

        plan = upstream.plan_reconcile(theirs)

        assert derived in plan.cascaded[MemoryType.SEMANTIC]
        assert derived not in plan.members(MemoryType.SEMANTIC)
        assert derived in plan.withdrawn[MemoryType.SEMANTIC]

    async def test_r1_holds_after_the_reconciliation(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """``verify`` would not catch this: it recomputes hashes and compositions, not citations across
        modules. That is the same reason admitting a rejected block is refused."""
        upstream, theirs, derived = await self.orphaning(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="taking their withdrawal")
        upstream.reconcile_accept_removals(reason="the source is gone, so what rests on it goes too")
        upstream.reconcile_continue()

        canonical = set(upstream.module(MemoryType.CANONICAL).block_ids)
        semantic = set(upstream.module(MemoryType.SEMANTIC).block_ids)
        ledger = Ledger.of(upstream.modules())

        assert derived not in semantic
        assert not [block for block in semantic for cited in ledger.evidence.get(block, []) if cited not in canonical]
        assert upstream.verify()

    def test_a_replay_removes_them_at_the_step_that_withdrew_the_evidence(self, tmp_path: Path) -> None:
        """A rebase replays their history one version at a time, so the consequence belongs to the version
        that caused it.

        Applying the whole contribution's cascade to every step would publish versions excluding a block
        whose evidence they still hold, with nothing recording why -- an unexplained removal, which is the
        one thing an auditable history cannot contain. Two handles on one layout, because only a store that
        holds the intermediate compositions can replay them at all.
        """
        layout = tmp_path / "brain"
        ours = Brain.open(layout, actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        ours.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("Fourier"))
        source = next(iter(ours.module(MemoryType.CANONICAL).block_ids))

        theirs = Brain.open(layout, actor=CONTRIBUTOR, policy=PERMISSIVE_POLICY)
        theirs.ingest(b"%PDF-1.7 Lecture 08", paper(CONTRIBUTOR), llm("Laplace"))
        theirs.drop(
            DropRequest(
                blocks=[source],
                memory_type=MemoryType.CANONICAL,
                actor=CONTRIBUTOR,
                reason="the lecture was withdrawn",
            )
        )
        contributed = theirs.snapshot().digest

        # Derived here, from the evidence they went on to withdraw.
        task = ours.define_task(source, allowed=[MemoryType.SEMANTIC])
        ours.commit(
            ours.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.SEMANTIC,
                            evidence=[source],
                            payload={
                                "kind": "fact",
                                "label": "a later reading",
                                "statement": "derived from the source",
                                "subject": "signals",
                            },
                        )
                    ],
                ),
                task,
            )
        )
        semantic = ours.module(MemoryType.SEMANTIC)
        derived = next(block for block in semantic.block_ids if semantic.get(block).label == "a later reading")
        assert ours.plan_reconcile(contributed).replayable > 1

        with pytest.raises(ReconciliationHaltedError):
            ours.rebase(contributed, reason="replay their versions onto mine")
        ours.reconcile_accept_removals(reason="the source is gone, so what rests on it goes too")
        result = ours.reconcile_continue()

        for digest in result.snapshots:
            version = snapshot_at(ours.store, digest)
            held = set(composition_at(ours.store, version, MemoryType.CANONICAL) or [])
            kept = set(composition_at(ours.store, version, MemoryType.SEMANTIC) or [])
            provenance = composition_at(ours.store, version, MemoryType.PROVENANCE)
            recorded = Ledger.of({MemoryType.PROVENANCE: Module(MemoryType.PROVENANCE, ours.store, provenance)}).removed

            # Either the evidence is still there and so is what cites it, or both are gone and the record
            # that took them is in this same version.
            assert (source in held) == (derived in kept)
            assert (derived in recorded) == (derived not in kept)

        assert derived not in set(ours.module(MemoryType.SEMANTIC).block_ids)
        assert ours.verify()

    async def test_the_cascade_records_why_it_removed_them(self, tmp_path: Path, registry: LocalLayoutRegistry) -> None:
        """The other history recorded withdrawing the evidence; nothing yet recorded the consequence here,
        and an unexplained removal is not auditable."""
        upstream, theirs, derived = await self.orphaning(tmp_path, registry)
        with pytest.raises(ReconciliationHaltedError):
            upstream.merge(theirs, reason="taking their withdrawal")
        upstream.reconcile_accept_removals(reason="accepted by the maintainer")
        upstream.reconcile_continue()

        assert derived in Ledger.of(upstream.modules()).removed


class TestGovernanceConflict:
    """Two histories carrying different trust roots are never reconciled automatically."""

    def test_a_differing_trust_root_is_refused_as_a_governance_act(self, tmp_path: Path) -> None:
        from boltzmann.authenticity import Scope, SshPublicKey, TrustedKey, TrustRoot, put_string
        from boltzmann.exceptions import GovernanceConflictError

        brain = Brain.open(tmp_path / "brain", actor=MAINTAINER, policy=PERMISSIVE_POLICY)
        brain.ingest(b"%PDF-1.7 Lecture 07", paper(), llm("Fourier"))
        blob = put_string(b"ssh-ed25519") + put_string(bytes(32))
        theirs_root = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(TrustedKey(key=SshPublicKey.from_blob(blob), scopes=(Scope.GOVERN,), since=1),),
        )
        theirs = brain.snapshot().with_trust_root(theirs_root)
        brain.store.put_bytes(theirs.canonical_bytes())

        with pytest.raises(GovernanceConflictError, match="union of both sides"):
            brain.plan_reconcile(theirs.digest)
        with pytest.raises(GovernanceConflictError):
            brain.merge(theirs.digest, reason="should never get this far")
