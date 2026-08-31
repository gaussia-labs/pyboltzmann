"""Querying a brain: filter, resolve, verify.

What is asserted here is the contract of Section 9.2 -- data with provenance, never prose, every match
verified against the installed snapshot -- and the filters the query declares. What is deliberately
*not* asserted is ranking order beyond term coverage: the protocol guarantees verifiability, not
identical ranking, so a test that pinned an order would be testing an implementation detail the paper
leaves open.
"""

from pathlib import Path

import pytest

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.exceptions import QueryError
from boltzmann.identity.digest import BlockId
from boltzmann.indices.base import AbstractIndex, IndexKind
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.ingest.validation import ValidationStatus
from boltzmann.query.evidence import EvidenceBundle
from boltzmann.query.request import Query, RetrievalMode
from boltzmann.query.scan import STOPWORDS, ProvenanceView, content_terms, searchable_text
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator@example.org", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="some-model", version="1")
PDF = b"%PDF-1.7 Lecture 07: a periodic function decomposes into sines and cosines"

CONTENT = [
    (
        MemoryType.SEMANTIC,
        {
            "kind": "formula",
            "label": "Fourier series",
            "statement": "decomposes a periodic function into sines",
            "subject": "signals",
        },
        "p.147",
    ),
    (
        MemoryType.SEMANTIC,
        {"kind": "concept", "label": "Periodicity", "statement": "repeats at intervals", "subject": "signals"},
        None,
    ),
    (
        MemoryType.SEMANTIC,
        {"kind": "fact", "label": "Orthogonality", "statement": "sines are orthogonal", "subject": "algebra"},
        None,
    ),
    (
        MemoryType.PROCEDURAL,
        {"label": "Compute coefficients", "goal": "obtain a_n", "steps": [{"action": "integrate against cos"}]},
        None,
    ),
    (
        MemoryType.EPISODIC,
        {
            "summary": "Lecture 07 covered Fourier coefficients",
            "occurred_at": "2026-05-14T14:00:00Z",
            "context": "Signals and Systems",
            "tags": ["lecture", "theory"],
        },
        None,
    ),
]


@pytest.fixture
def brain() -> Brain:
    brain = Brain(MemoryBlockStore(), actor=CURATOR)
    request = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
    source = brain.register(PDF, request).block_id
    task = brain.define_task(source)
    proposals = CandidateSet(
        producer=MODEL,
        candidates=[
            Candidate(memory_type=kind, evidence=[source], locator=locator, payload=payload)
            for kind, payload, locator in CONTENT
        ],
    )
    report = brain.validate(proposals, task)
    assert report.is_clean, [issue.detail for r in report.results for issue in r.issues]
    brain.commit(report)
    return brain


@pytest.fixture
def source(brain: Brain) -> BlockId:
    return brain.module(MemoryType.CANONICAL).block_ids[0]


def labels(bundle: EvidenceBundle) -> set[str]:
    return {
        match.content.get("label") or match.content.get("summary") or match.content.get("media_type", "")
        for match in bundle.matches
    }


class TestContract:
    """What Section 9.2 requires of any result."""

    def test_returns_data_not_prose(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="Fourier"))
        assert isinstance(bundle, EvidenceBundle)
        assert bundle.matches[0].content["label"] == "Fourier series"

    def test_every_match_is_verified(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="sines"))
        assert bundle.matches
        assert bundle.all_verified
        bundle.require_verified()

    def test_reports_the_roots_it_verified_against(self, brain: Brain) -> None:
        """``verified: true`` has to be a checkable claim about a named snapshot."""
        bundle = brain.search(Query(text="Fourier"))
        for memory_type, root in bundle.verified_against.items():
            assert root == brain.root_of(memory_type)

    def test_every_match_carries_its_provenance(self, brain: Brain, source: BlockId) -> None:
        bundle = brain.search(Query(text="Fourier"))
        match = bundle.matches[0]
        assert match.sources[0].block_id == source
        assert match.sources[0].locator == "p.147"

    def test_scores_are_strings(self, brain: Brain) -> None:
        assert all(isinstance(match.score, str) for match in brain.search(Query(text="sines")).matches)

    def test_no_match_is_a_legitimate_answer(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="quantum chromodynamics"))
        assert len(bundle) == 0
        assert bundle.all_verified

    def test_an_empty_brain_answers_without_error(self) -> None:
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        assert len(brain.search(Query(text="anything"))) == 0


class TestTermMatching:
    """Coverage, not relevance, and it says so."""

    def test_matches_across_fields(self, brain: Brain) -> None:
        assert labels(brain.search(Query(text="orthogonal"))) == {"Orthogonality"}
        assert labels(brain.search(Query(text="Signals and Systems"))) >= {"Lecture 07 covered Fourier coefficients"}

    def test_is_case_insensitive(self, brain: Brain) -> None:
        assert labels(brain.search(Query(text="FOURIER"))) == labels(brain.search(Query(text="fourier")))

    def test_partial_coverage_scores_lower(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="periodic orthogonal"))
        scores = {match.content["label"]: match.score for match in bundle.matches}
        assert scores["Fourier series"] == "0.50"
        assert scores["Orthogonality"] == "0.50"

    def test_full_coverage_ranks_first(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="sines periodic"))
        assert bundle.matches[0].content["label"] == "Fourier series"
        assert bundle.matches[0].score == "1.00"

    def test_canonical_blocks_carry_no_prose(self, brain: Brain, source: BlockId) -> None:
        """Canonical memory records what was observed, not what it says."""
        block = brain.module(MemoryType.CANONICAL).get(source)
        assert searchable_text(block) == ["application/pdf"]
        assert "Fourier series" not in labels(brain.search(Query(text="application/pdf")))


class TestFunctionWords:
    """A filter that admits nearly everything is not filtering.

    The scan counted every whitespace-separated word as a term, so a query containing ``an`` matched any
    block whose text contained ``an`` anywhere -- fourteen of fifteen, in a brain that knew nothing about
    the subject asked for. Every one of them then carried a score, because coverage was above zero.
    """

    def test_a_query_the_brain_knows_nothing_about_matches_nothing(self, brain: Brain) -> None:
        assert brain.search(Query(text="thermodynamic entropy of an ideal gas")).matches == []

    def test_a_query_of_only_function_words_falls_back_to_them(self, brain: Brain) -> None:
        """Deliberate. Dropping every term would answer "what is it" with nothing found, which is worse
        than answering it badly -- and a caller who typed only function words gave nothing to narrow by."""
        assert content_terms("of an the") == ["of", "an", "the"]
        assert brain.search(Query(text="of an the")).matches

    def test_the_denominator_counts_only_content_words(self, brain: Brain) -> None:
        """Which is what fixes the ranking, not just the filtering: a block stops being rewarded for
        sharing grammar."""
        assert content_terms("what is the periodic function of a signal") == ["periodic", "function", "signal"]

        bundle = brain.search(Query(text="what is a periodic function"))
        assert bundle.matches[0].content["label"] == "Fourier series"
        assert bundle.matches[0].score == "1.00"

    def test_the_list_is_grammar_rather_than_frequency(self) -> None:
        """A list built from frequency eventually swallows a term some brain treats as knowledge."""
        assert "the" in STOPWORDS
        assert "index" not in STOPWORDS
        assert "memory" not in STOPWORDS
        assert "block" not in STOPWORDS


class TestFilters:
    """Narrowing conditions, including the ones that need no query terms at all."""

    def test_memory_types(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="Fourier", filters={"memory_types": [MemoryType.EPISODIC]}))
        assert {match.memory_type for match in bundle.matches} == {MemoryType.EPISODIC}

    def test_a_filter_only_query_is_valid(self, brain: Brain) -> None:
        """ "The episodes of last May" is a complete request with no terms in it."""
        query = Query(filters={"subject": "signals"})
        assert query.is_filter_only
        assert labels(brain.search(query)) == {"Fourier series", "Periodicity"}

    def test_subject(self, brain: Brain) -> None:
        assert labels(brain.search(Query(filters={"subject": "algebra"}))) == {"Orthogonality"}

    def test_tags(self, brain: Brain) -> None:
        assert labels(brain.search(Query(filters={"tags": ["theory"]}))) == {"Lecture 07 covered Fourier coefficients"}

    def test_tags_must_all_be_present(self, brain: Brain) -> None:
        assert len(brain.search(Query(filters={"tags": ["theory", "absent"]}))) == 0

    def test_recency_window(self, brain: Brain) -> None:
        within = Query(filters={"since": "2026-05-01T00:00:00Z", "until": "2026-05-31T00:00:00Z"})
        after = Query(filters={"since": "2026-06-01T00:00:00Z"})
        assert len(brain.search(within)) == 1
        assert len(brain.search(after)) == 0

    def test_a_recency_window_excludes_timeless_blocks(self, brain: Brain) -> None:
        """A semantic fact has no time, so it cannot satisfy a window rather than defaulting into it."""
        bundle = brain.search(Query(filters={"since": "2000-01-01T00:00:00Z"}))
        assert {match.memory_type for match in bundle.matches} == {MemoryType.EPISODIC}

    def test_cited_evidence(self, brain: Brain, source: BlockId) -> None:
        bundle = brain.search(Query(filters={"evidence": [source]}, hints={"limit": 50}))
        assert len(bundle) == len(CONTENT)

    def test_unknown_evidence_matches_nothing(self, brain: Brain) -> None:
        assert len(brain.search(Query(filters={"evidence": [BlockId.of(b"absent")]}))) == 0

    def test_an_uninstalled_module_in_the_filter_is_ignored(self, brain: Brain) -> None:
        """A partial install must not turn a filter into an error."""
        bundle = brain.search(Query(filters={"memory_types": [MemoryType.PROVENANCE, MemoryType.SEMANTIC]}))
        assert MemoryType.SEMANTIC in bundle.verified_against


class TestLimit:
    def test_truncation_is_reported(self, brain: Brain, source: BlockId) -> None:
        bundle = brain.search(Query(filters={"evidence": [source]}, hints={"limit": 2}))
        assert len(bundle) == 2
        assert bundle.truncated

    def test_a_complete_answer_is_not_marked_truncated(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="orthogonal", hints={"limit": 10}))
        assert not bundle.truncated


class TestExactMode:
    def test_resolves_an_identity(self, brain: Brain) -> None:
        block_id = brain.module(MemoryType.SEMANTIC).block_ids[0]
        bundle = brain.search(Query(text=str(block_id), hints={"mode": RetrievalMode.EXACT}))
        assert [match.block_id for match in bundle.matches] == [block_id]
        assert bundle.matches[0].score == "1.00"

    def test_an_absent_identity_matches_nothing(self, brain: Brain) -> None:
        query = Query(text=str(BlockId.of(b"absent")), hints={"mode": RetrievalMode.EXACT})
        assert len(brain.search(query)) == 0

    def test_a_non_digest_matches_nothing(self, brain: Brain) -> None:
        assert len(brain.search(Query(text="Fourier", hints={"mode": RetrievalMode.EXACT}))) == 0


class TestAssociativeExpansion:
    """Relations live on the block, so following them needs no graph engine."""

    def test_expands_along_declared_relations(self) -> None:
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        request = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        source = brain.register(PDF, request).block_id
        task = brain.define_task(source)

        anchor = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={"kind": "concept", "label": "Periodicity", "statement": "repeats"},
        )
        brain.commit(brain.validate(CandidateSet(producer=MODEL, candidates=[anchor]), task))
        target = brain.module(MemoryType.SEMANTIC).block_ids[0]

        linked = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={
                "kind": "formula",
                "label": "Fourier series",
                "statement": "unrelated wording",
                "relations": [{"predicate": "depends_on", "target": str(target)}],
            },
        )
        brain.commit(brain.validate(CandidateSet(producer=MODEL, candidates=[linked]), task))

        without = brain.search(Query(text="Fourier"))
        assert labels(without) == {"Fourier series"}

        with_expansion = brain.search(Query(text="Fourier", hints={"expand_depth": 1}))
        assert labels(with_expansion) == {"Fourier series", "Periodicity"}

    def test_a_block_reached_by_association_carries_no_coverage(self) -> None:
        """It was not matched, so claiming term coverage for it would be a lie."""
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        request = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        source = brain.register(PDF, request).block_id
        task = brain.define_task(source)
        brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.SEMANTIC,
                            evidence=[source],
                            payload={"kind": "concept", "label": "Periodicity", "statement": "repeats"},
                        )
                    ],
                ),
                task,
            )
        )
        target = brain.module(MemoryType.SEMANTIC).block_ids[0]
        brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.SEMANTIC,
                            evidence=[source],
                            payload={
                                "kind": "formula",
                                "label": "Fourier series",
                                "statement": "x",
                                "relations": [{"predicate": "depends_on", "target": str(target)}],
                            },
                        )
                    ],
                ),
                task,
            )
        )
        bundle = brain.search(Query(text="Fourier", hints={"expand_depth": 1}))
        scores = {match.content["label"]: match.score for match in bundle.matches}
        assert scores["Fourier series"] == "1.00"
        assert scores["Periodicity"] == "0.00"


class TestSelfDescribingBlocks:
    """A derived block states its own citations, so a partial install can still see what it rests on."""

    def test_the_candidates_evidence_lands_on_the_block(self, brain: Brain, source: BlockId) -> None:
        block = brain.module(MemoryType.SEMANTIC).get(brain.module(MemoryType.SEMANTIC).block_ids[0])
        assert block.evidence == [source]

    def test_a_payload_that_contradicts_its_citation_is_rejected(self) -> None:
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        request = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        source = brain.register(PDF, request).block_id
        task = brain.define_task(source)

        inconsistent = Candidate(
            memory_type=MemoryType.SEMANTIC,
            evidence=[source],
            payload={
                "kind": "fact",
                "label": "L",
                "statement": "S",
                "evidence": [str(BlockId.of(b"something else"))],
            },
        )
        result = brain.validate(CandidateSet(candidates=[inconsistent]), task).results[0]
        assert result.status is ValidationStatus.REJECTED
        assert result.issues[0].code == "evidence-mismatch"


class TestSupersession:
    """Supersession changes accessibility, not membership (paper Section 10.4)."""

    def test_a_superseded_block_is_held_back_by_default(self, brain: Brain) -> None:
        from boltzmann.blocks.provenance import ProvenanceBlock, SupersessionRecord
        from boltzmann.identity.time import utc_timestamp

        semantic = brain.module(MemoryType.SEMANTIC)
        old = next(b for b in semantic.blocks() if b.label == "Periodicity")
        new = next(b for b in semantic.blocks() if b.label == "Orthogonality")
        record = ProvenanceBlock(
            record=SupersessionRecord(
                block=new.block_id,
                supersedes=old.block_id,
                actor=CURATOR,
                at=utc_timestamp(),
            )
        )
        brain._write(blocks={}, provenance=[record])

        view = ProvenanceView.of(brain.modules())
        assert view.superseded_by[old.block_id] == new.block_id

        assert "Periodicity" not in labels(brain.search(Query(filters={"subject": "signals"})))
        assert "Periodicity" in labels(brain.search(Query(filters={"subject": "signals", "include_superseded": True})))

    def test_the_superseded_block_still_belongs_to_the_composition(self, brain: Brain) -> None:
        """Accessibility changed; membership did not."""
        semantic = brain.module(MemoryType.SEMANTIC)
        old = next(b for b in semantic.blocks() if b.label == "Periodicity")
        assert old.block_id in brain.module(MemoryType.SEMANTIC)
        assert brain.prove(old.block_id, MemoryType.SEMANTIC).verify(brain.root_of(MemoryType.SEMANTIC))


class TestRedactedBlocks:
    """A tombstoned block is reported, never silently dropped or silently returned."""

    def test_a_tombstoned_match_is_marked_unresolvable(self, brain: Brain) -> None:
        block_id = next(b.block_id for b in brain.module(MemoryType.SEMANTIC).blocks() if b.label == "Orthogonality")
        brain.store.tombstone(block_id, "erasure policy")

        bundle = brain.search(Query(filters={"memory_types": [MemoryType.SEMANTIC]}, hints={"limit": 50}))
        tombstoned = [match for match in bundle.matches if match.block_id == block_id]
        assert tombstoned == [] or not tombstoned[0].resolvable

    def test_resolvability_separates_tombstoned_from_missing(self, brain: Brain) -> None:
        block_id = next(b.block_id for b in brain.module(MemoryType.SEMANTIC).blocks() if b.label == "Orthogonality")
        brain.store.tombstone(block_id, "erasure policy")

        report = brain.resolvability()
        assert block_id in report.tombstoned[MemoryType.SEMANTIC]
        assert block_id not in report.resolvable.get(MemoryType.SEMANTIC, [])
        assert not report.missing

    def test_an_intact_brain_reports_nothing_missing(self, brain: Brain) -> None:
        report = brain.resolvability()
        assert report.is_intact
        assert sum(len(ids) for ids in report.resolvable.values()) == brain.snapshot().block_count


class TestPlannerDelegation:
    """A planner replaces candidate generation; the SDK does not argue with it."""

    def test_an_injected_planner_is_used(self) -> None:
        sentinel = EvidenceBundle(truncated=True)

        class Fixed:
            def __init__(self) -> None:
                self.calls = 0

            def plan(self, query: Query, modules: dict) -> EvidenceBundle:
                self.calls += 1
                return sentinel

        planner = Fixed()
        brain = Brain(MemoryBlockStore(), actor=CURATOR, planner=planner)
        result = brain.search(Query(text="anything"))
        # The planner's bundle comes back enriched with the authorship line, which is the
        # protocol's to add -- everything the planner produced is untouched.
        assert result.model_copy(update={"authorship": None}) == sentinel
        assert result.authorship is not None
        assert planner.calls == 1


class TestOpenIndex:
    """The SDK ships no engine, so an index exists only if the caller supplied one."""

    def test_an_unregistered_index_is_refused_with_guidance(self, brain: Brain) -> None:
        with pytest.raises(QueryError, match="ships no index engine"):
            brain.open_index(MemoryType.SEMANTIC, IndexKind.VECTOR)

    def test_a_registered_index_is_returned(self) -> None:
        class Counting(AbstractIndex):
            KIND = IndexKind.HASH_MAP

            def build(self, blocks, content):
                self.count = sum(1 for _ in blocks)

            def search(self, query, limit=10):
                return []

        index = Counting()
        brain = Brain(MemoryBlockStore(), actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})
        assert brain.open_index(MemoryType.SEMANTIC, IndexKind.HASH_MAP) is index

    def test_indices_are_rebuilt_on_commit(self) -> None:
        """Indices are derived, so a commit rebuilds them from the composition rather than patching."""

        class Counting(AbstractIndex):
            KIND = IndexKind.HASH_MAP

            def __init__(self) -> None:
                self.count = 0
                self.rebuilds = 0

            def build(self, blocks, content):
                self.count = sum(1 for _ in blocks)
                self.rebuilds += 1

            def search(self, query, limit=10):
                return []

        index = Counting()
        brain = Brain(MemoryBlockStore(), actor=CURATOR, indices={MemoryType.SEMANTIC: [index]})
        request = RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        source = brain.register(PDF, request).block_id
        task = brain.define_task(source)
        brain.commit(
            brain.validate(
                CandidateSet(
                    producer=MODEL,
                    candidates=[
                        Candidate(
                            memory_type=MemoryType.SEMANTIC,
                            evidence=[source],
                            payload={"kind": "fact", "label": "L", "statement": "S"},
                        )
                    ],
                ),
                task,
            )
        )
        assert index.rebuilds == 1
        assert index.count == 1


class TestProvenanceView:
    def test_an_empty_brain_yields_an_empty_view(self) -> None:
        assert ProvenanceView.of({}).locators == {}

    def test_a_partial_install_yields_an_empty_view(self, tmp_path: Path) -> None:
        """No provenance module is legitimate, not an error."""
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        assert ProvenanceView.of(brain.modules()).superseded_by == {}


class TestAuthorshipInTheBundle:
    """The second verification rides beside the first, and is never folded into it."""

    def test_an_ungoverned_brain_reports_unsigned_not_nothing(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="Fourier"))
        assert bundle.authorship is not None
        assert bundle.authorship.state.value == "unsigned"
        assert bundle.authorship.snapshot == brain.snapshot().digest

    def test_authorship_does_not_touch_verified(self, brain: Brain) -> None:
        bundle = brain.search(Query(text="Fourier"))
        assert bundle.all_verified, "hash-and-membership verification is its own fact"
        assert bundle.authorship is not None
        assert bundle.authorship.state.value == "unsigned"

    def test_a_signed_brain_reports_authorized_evidence(self, tmp_path) -> None:
        ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
        serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
        from boltzmann.authenticity import Scope, SshPublicKey, TrustedKey, TrustRoot, rfc4253_signature

        private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x61]) * 32)
        line = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)

        class Party:
            public_key = SshPublicKey.parse(line.decode("ascii"))

            @staticmethod
            def sign_blob(data: bytes) -> bytes:
                return rfc4253_signature("ssh-ed25519", private.sign(data))

        root = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(TrustedKey(key=Party.public_key, scopes=(Scope.INGEST, Scope.COMMIT, Scope.GOVERN), since=1),),
        )
        governed = Brain.init(tmp_path / "governed", actor=CURATOR, trust_root=root, signers=[Party()])
        bundle = governed.search(Query(text="anything"))
        assert bundle.authorship is not None
        assert bundle.authorship.state.value == "authorized"
        assert bundle.authorship.key == Party.public_key.fingerprint
        assert bundle.authorship.trust_root == root.digest

    def test_the_chain_walk_is_paid_once_per_head(self, brain: Brain, monkeypatch) -> None:
        from boltzmann.authenticity.authenticator import Authenticator

        calls = {"count": 0}
        original = Authenticator.authenticate

        def counting(self, snapshot, records=None, current=None, **kwargs):
            calls["count"] += 1
            return original(self, snapshot, records=records, current=current, **kwargs)

        monkeypatch.setattr(Authenticator, "authenticate", counting)
        brain.search(Query(text="Fourier"))
        brain.search(Query(text="series"))
        assert calls["count"] == 1, "the second query must not pay for the walk the first one paid"
