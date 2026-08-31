"""What a planner may decide, and what it may not.

The protocol fixes the contract, not the algorithm: return blocks with their provenance and a score, verify
every one against the installed snapshot, treat no index as authoritative. Ranking is the planner's to get
wrong. Verification is not -- so the tests that matter here are the ones that would catch a planner
returning something it did not check, or something the query excluded.
"""

from __future__ import annotations

import pytest
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.query.planner import QueryPlanner
from boltzmann.query.request import Query, QueryFilters, QueryHints, RetrievalMode
from boltzmann.store.memory import MemoryBlockStore

from boltzmann_sandbox.indices import InvertedIndex, VectorIndex
from boltzmann_sandbox.planner import RRF_K, HybridPlanner

CURATOR = Actor(id="curator@example.org", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="test", version="1")

FACTS = [
    ("formula", "Fourier series", "decomposes a periodic function into sines and cosines", "signals"),
    ("concept", "Laplace transform", "maps a function of time into a function of complex frequency", "signals"),
    ("fact", "Nyquist rate", "sampling must exceed twice the highest frequency", "signals"),
    ("concept", "Convolution theorem", "multiplication in frequency equals convolution in time", "signals"),
    ("concept", "Gibbs phenomenon", "truncating a series overshoots near a discontinuity", "analysis"),
]


@pytest.fixture
def brain() -> Brain:
    """A brain with the sandbox's planner and indices, holding five semantic blocks."""
    indices: dict = {MemoryType.SEMANTIC: [InvertedIndex(), VectorIndex()]}
    instance = Brain(
        MemoryBlockStore(),
        actor=CURATOR,
        planner=HybridPlanner(indices),
        indices=indices,
    )
    source = instance.register(
        b"%PDF-1.7 signals lecture", RegistrationRequest(media_type="application/pdf", actor=CURATOR)
    )
    task = instance.define_task(source.block_id, allowed=[MemoryType.SEMANTIC])
    instance.commit(
        instance.validate(
            CandidateSet(
                producer=MODEL,
                candidates=[
                    Candidate(
                        memory_type=MemoryType.SEMANTIC,
                        evidence=[source.block_id],
                        locator=f"p.{position}",
                        payload={"kind": kind, "label": label, "statement": statement, "subject": subject},
                    )
                    for position, (kind, label, statement, subject) in enumerate(FACTS, start=1)
                ],
            ),
            task,
        )
    )
    return instance


def labels(bundle) -> list[str]:
    """The labels a bundle returned, in the order it returned them."""
    return [match.content["label"] for match in bundle.matches]


class TestTheContract:
    def test_it_satisfies_the_interface(self) -> None:
        assert isinstance(HybridPlanner(), QueryPlanner)

    def test_every_match_is_verified(self, brain: Brain) -> None:
        """The one thing a planner does not get to decide."""
        bundle = brain.search(Query(text="frequency"))
        assert bundle.matches
        assert bundle.all_verified
        assert all(match.verified for match in bundle.matches)

    def test_matches_carry_their_provenance(self, brain: Brain) -> None:
        """Data with its sources, never prose. There is no answer field to fill in."""
        bundle = brain.search(Query(text="periodic function"))
        assert bundle.matches[0].sources
        assert bundle.matches[0].sources[0].locator is not None
        assert not hasattr(bundle, "answer")

    def test_the_bundle_says_which_roots_it_was_verified_against(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="frequency"))
        assert bundle.verified_against[MemoryType.SEMANTIC] == brain.root_of(MemoryType.SEMANTIC)


class TestRanking:
    def test_the_best_match_comes_first(self, brain: Brain) -> None:
        assert labels(brain.search(Query(text="periodic function sines")))[0] == "Fourier series"

    def test_a_distinctive_term_finds_its_block(self, brain: Brain) -> None:
        assert labels(brain.search(Query(text="discontinuity overshoot")))[0] == "Gibbs phenomenon"

    def test_scores_are_ordered_with_the_results(self, brain: Brain) -> None:
        """A caller that sorts by score has to get the order the planner returned, or the score is a trap."""
        bundle = brain.search(Query(text="frequency time"))
        scores = [float(match.score) for match in bundle.matches]
        assert scores == sorted(scores, reverse=True)

    def test_scores_stay_within_zero_and_one(self, brain: Brain) -> None:
        for match in brain.search(Query(text="frequency")).matches:
            assert 0.0 <= float(match.score) <= 1.0

    def test_the_score_is_a_string(self, brain: Brain) -> None:
        """A wire format should not carry a float, and the SDK's own scan agrees."""
        assert isinstance(brain.search(Query(text="frequency")).matches[0].score, str)

    def test_the_top_score_is_one_when_every_strategy_agrees(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="truncating a series overshoots near a discontinuity"))
        assert bundle.matches[0].score == "1.0000"

    def test_the_offset_is_small_enough_to_spread_the_scores(self, brain: Brain) -> None:
        """With RRF's classic k=60 every result lands within two percent of every other at this size, so
        the ordering is right and the score says nothing."""
        assert RRF_K < 10
        bundle = brain.search(Query(text="frequency time"))
        spread = float(bundle.matches[0].score) - float(bundle.matches[-1].score)
        assert spread > 0.05


class TestWhatItMayNotReturn:
    def test_the_limit_is_respected(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="frequency", hints=QueryHints(limit=2)))
        assert len(bundle.matches) == 2
        assert bundle.truncated

    def test_a_memory_type_filter_is_honoured(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="pdf", filters=QueryFilters(memory_types=[MemoryType.CANONICAL])))
        assert all(match.memory_type is MemoryType.CANONICAL for match in bundle.matches)

    def test_a_subject_filter_excludes_what_the_indices_still_know(self, brain: Brain) -> None:
        """The indices see every block; the scan's filtering is what decides eligibility, and fusion only
        reorders what survived it."""
        bundle = brain.search(Query(text="series", filters=QueryFilters(subject="analysis")))
        assert labels(bundle) == ["Gibbs phenomenon"]

    def test_a_superseded_block_is_held_back_by_default(self, brain: Brain) -> None:
        blocks = brain.module(MemoryType.SEMANTIC).block_ids
        newer, older = blocks[0], blocks[1]
        brain.supersede(newer, older, MemoryType.SEMANTIC, reason="corrected")

        visible = brain.search(Query(text="frequency time series function", hints=QueryHints(limit=10)))
        assert older not in [match.block_id for match in visible.matches]

        asked = brain.search(
            Query(
                text="frequency time series function",
                filters=QueryFilters(include_superseded=True),
                hints=QueryHints(limit=10),
            )
        )
        assert older in [match.block_id for match in asked.matches]

    def test_an_exact_lookup_is_not_reranked(self, brain: Brain) -> None:
        """An identity is not a ranked guess, so fusing it with approximate rankings would be wrong."""
        wanted = brain.module(MemoryType.SEMANTIC).block_ids[0]
        bundle = brain.search(Query(text=str(wanted), hints=QueryHints(mode=RetrievalMode.EXACT)))
        assert [match.block_id for match in bundle.matches] == [wanted]
        assert bundle.matches[0].score == "1.00"

    def test_nothing_matches_a_query_about_something_else(self, brain: Brain) -> None:
        assert brain.search(Query(text="thermodynamics entropy enthalpy")).matches == []


class TestWithoutIndices:
    def test_it_degrades_to_the_scan_rather_than_failing(self, brain: Brain) -> None:
        """A planner with no index is still conforming -- just not hybrid."""
        bare = HybridPlanner()
        bundle = bare.plan(Query(text="periodic function"), brain.modules())
        assert bundle.matches
        assert bundle.all_verified

    def test_the_limit_still_applies(self, brain: Brain) -> None:
        bare = HybridPlanner()
        bundle = bare.plan(Query(text="frequency", hints=QueryHints(limit=1)), brain.modules())
        assert len(bundle.matches) == 1
