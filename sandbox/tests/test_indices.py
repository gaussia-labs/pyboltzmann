"""The two index engines, and the contract the travelling one has to satisfy.

Most of what matters here is not "does it rank well" -- these are examples, and their ranking is crude on
purpose -- but whether they satisfy the interface honestly. An index that claims to be rebuildable and is
not, or one whose dump does not round-trip, breaks a consumer rather than itself.
"""

from __future__ import annotations

import math

import pytest
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind, Producer, ProducerKind
from boltzmann.brain import Brain
from boltzmann.indices.base import Index, IndexKind, TravellingIndex
from boltzmann.ingest.proposer import Candidate, CandidateSet
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.store.memory import MemoryBlockStore

from boltzmann_sandbox.indices import (
    MIN_STEM_LENGTH,
    MIN_TOKEN_LENGTH,
    IndexFormatError,
    InvertedIndex,
    VectorIndex,
    block_tokens,
    stem,
    tokenize,
)

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)
MODEL = Producer(kind=ProducerKind.MODEL, id="test", version="1")

FACTS = [
    ("Fourier series", "decomposes a periodic function into sines and cosines"),
    ("Laplace transform", "maps a function of time into a function of complex frequency"),
    ("Nyquist rate", "sampling must exceed twice the highest frequency"),
]


def index_of(blocks: list) -> VectorIndex:
    """A vector index built over these blocks."""
    index = VectorIndex()
    index.build(blocks)
    return index


@pytest.fixture
def blocks() -> list:
    """Three semantic blocks, built through the SDK so they are real blocks and not stand-ins."""
    brain = Brain(MemoryBlockStore(), actor=CURATOR)
    source = brain.register(b"%PDF-1.7 signals", RegistrationRequest(media_type="application/pdf", actor=CURATOR))
    task = brain.define_task(source.block_id, allowed=[MemoryType.SEMANTIC])
    brain.commit(
        brain.validate(
            CandidateSet(
                producer=MODEL,
                candidates=[
                    Candidate(
                        memory_type=MemoryType.SEMANTIC,
                        evidence=[source.block_id],
                        payload={"kind": "fact", "label": label, "statement": statement, "subject": "signals"},
                    )
                    for label, statement in FACTS
                ],
            ),
            task,
        )
    )
    return list(brain.module(MemoryType.SEMANTIC).blocks())


class TestTokenizing:
    def test_case_is_folded(self) -> None:
        assert tokenize("Fourier SERIES") == tokenize("fourier series")

    def test_punctuation_separates(self) -> None:
        assert tokenize("sin(x), cos(x)") == ["sin", "cos"]

    def test_single_characters_are_dropped(self) -> None:
        """They carry no retrieval signal and appear in nearly every block."""
        assert MIN_TOKEN_LENGTH == 2
        assert tokenize("a periodic function f") == ["periodic", "function"]


class TestStemming:
    """One word and its inflections have to become one token, or neither index credits the right block."""

    @pytest.mark.parametrize(
        ("word", "inflected"),
        [
            ("remove", "removing"),
            ("remove", "removes"),
            ("derive", "derived"),
            ("publish", "publishing"),
            ("version", "versions"),
            ("block", "blocks"),
            ("state", "states"),
        ],
    )
    def test_a_word_and_its_inflection_share_a_stem(self, word: str, inflected: str) -> None:
        assert stem(word) == stem(inflected)

    def test_the_trailing_e_is_what_closes_the_verb_family(self) -> None:
        """English drops it before -ing and -es, so without this rule a verb never matches itself."""
        assert stem("remove") == stem("removing") == "remov"

    def test_short_words_survive_intact(self) -> None:
        """Stripping below the floor collides everything: `ties` would become `t`."""
        assert MIN_STEM_LENGTH == 4
        for word in ("the", "this", "is", "has", "ties", "uses"):
            assert stem(word) == word

    def test_it_does_not_pretend_to_be_porter(self) -> None:
        """Irregular plurals and irregular verbs are out of reach of suffix stripping, and saying so is
        better than a rule that half works."""
        assert stem("indices") != stem("index")
        assert stem("was") != stem("be")

    def test_a_canonical_block_contributes_only_its_media_type(self, blocks: list) -> None:
        """It is a descriptor over bytes, not prose, so it must not match a natural-language query."""
        brain = Brain(MemoryBlockStore(), actor=CURATOR)
        registered = brain.register(
            b"%PDF-1.7 signals", RegistrationRequest(media_type="application/pdf", actor=CURATOR)
        )
        canonical = brain.resolve(registered.block_id)
        assert block_tokens(canonical) == ["application", "pdf"]


class TestTheInterface:
    def test_both_satisfy_index(self) -> None:
        assert isinstance(InvertedIndex(), Index)
        assert isinstance(VectorIndex(), Index)

    def test_only_the_vector_index_travels(self) -> None:
        assert not isinstance(InvertedIndex(), TravellingIndex)
        assert isinstance(VectorIndex(), TravellingIndex)

    def test_what_each_declares_about_rebuilding(self) -> None:
        """The distinction the protocol draws: can a client regenerate this without a model."""
        assert InvertedIndex().rebuildable
        assert InvertedIndex().model_tag is None
        assert not VectorIndex().rebuildable
        assert VectorIndex().model_tag == "sandbox-hashing-bow/2"

    def test_the_kinds_are_the_ones_they_serve(self) -> None:
        assert InvertedIndex().kind is IndexKind.INVERTED
        assert VectorIndex().kind is IndexKind.VECTOR


class TestInvertedIndex:
    def test_building_indexes_every_block(self, blocks: list) -> None:
        index = InvertedIndex()
        index.build(blocks)
        assert index.documents == len(FACTS)
        assert "fourier" in index.postings

    def test_rebuilding_discards_the_previous_composition(self, blocks: list) -> None:
        """A version is a set of blocks, so the index is rebuilt rather than patched."""
        index = InvertedIndex()
        index.build(blocks)
        index.build(blocks[:1])
        assert index.documents == 1

    def test_a_rare_term_outranks_a_common_one(self, blocks: list) -> None:
        """Which is the whole of what makes term matching useful."""
        index = InvertedIndex()
        index.build(blocks)
        ranked = index.search("fourier frequency")
        assert len(ranked) >= 2

        # "fourier" appears in one block, "frequency" in two, so the block carrying the rare term wins.
        best = ranked[0][0]
        winner = next(block for block in blocks if block.block_id == best)
        assert "fourier" in block_tokens(winner)

    def test_an_unknown_term_matches_nothing(self, blocks: list) -> None:
        index = InvertedIndex()
        index.build(blocks)
        assert index.search("thermodynamics") == []

    def test_an_empty_query_matches_nothing(self, blocks: list) -> None:
        index = InvertedIndex()
        index.build(blocks)
        assert index.search("") == []

    def test_searching_before_building_is_empty_rather_than_an_error(self) -> None:
        assert InvertedIndex().search("fourier") == []

    def test_the_limit_is_respected(self, blocks: list) -> None:
        index = InvertedIndex()
        index.build(blocks)
        assert len(index.search("function frequency sampling", limit=1)) == 1


class TestVectorIndex:
    def test_vectors_are_unit_length_to_within_the_rounding(self, blocks: list) -> None:
        """Unit length is what makes cosine similarity a dot product.

        Rounding each component to :attr:`VectorIndex.PRECISION` moves the norm slightly off 1 -- by at
        most ``2 * sqrt(DIMS) * 5e-7``, since a unit vector's components sum in absolute value to at most
        ``sqrt(DIMS)``. That is the price of a byte-reproducible dump, and it is small enough that a
        self-match scoring 1.000001 is the only visible consequence.
        """
        tolerance = 2 * math.sqrt(VectorIndex.DIMS) * 5 * 10**-VectorIndex.PRECISION
        for vector in index_of(blocks).vectors.values():
            assert sum(value * value for value in vector) == pytest.approx(1.0, abs=tolerance)

    def test_vectors_are_rounded_when_built(self, blocks: list) -> None:
        """Not only when dumped: a consumer that loaded the index must hold what the publisher holds, or
        the two ends rank with different numbers."""
        index = VectorIndex()
        index.build(blocks)
        for vector in index.vectors.values():
            assert all(value == round(value, VectorIndex.PRECISION) for value in vector)

    def test_a_block_matches_its_own_text_best(self, blocks: list) -> None:
        index = VectorIndex()
        index.build(blocks)
        for block in blocks:
            ranked = index.search(block.statement)
            assert ranked[0][0] == block.block_id

    def test_embedding_is_deterministic(self, blocks: list) -> None:
        """Two clients that indexed the same blocks have to produce the same bytes, or the layer digest
        would differ for identical content."""
        first, second = VectorIndex(), VectorIndex()
        first.build(blocks)
        second.build(blocks)
        assert first.dump() == second.dump()

    def test_the_dump_round_trips(self, blocks: list) -> None:
        index = VectorIndex()
        index.build(blocks)
        restored = VectorIndex()
        restored.load(index.dump())
        assert restored.vectors == index.vectors
        assert restored.dump() == index.dump()

    def test_an_index_from_another_model_is_refused(self, blocks: list) -> None:
        """Vectors from two models occupy different spaces, so the ranking would be meaningless."""
        index = VectorIndex()
        index.build(blocks)

        class Foreign(VectorIndex):
            MODEL_TAG = "some-other-model/9"

        with pytest.raises(IndexFormatError, match="different spaces"):
            Foreign().load(index.dump())

    def test_an_index_of_another_dimensionality_is_refused(self, blocks: list) -> None:
        index = VectorIndex()
        index.build(blocks)

        class Wider(VectorIndex):
            DIMS = 512

        with pytest.raises(IndexFormatError, match="dimensions"):
            Wider().load(index.dump())

    def test_bytes_that_are_not_a_dump_are_refused(self) -> None:
        with pytest.raises(IndexFormatError):
            VectorIndex().load(b"not json at all")

    def test_a_dump_missing_its_model_tag_is_refused(self) -> None:
        with pytest.raises(IndexFormatError):
            VectorIndex().load(b'{"dims": 256, "vectors": {}}')

    def test_searching_before_building_is_empty_rather_than_an_error(self) -> None:
        assert VectorIndex().search("fourier") == []
