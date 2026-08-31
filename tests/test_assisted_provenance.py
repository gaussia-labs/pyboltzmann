"""Most brains are hydrated through an agent, so the record of who did the work is the missing part.

A provenance record has always named an actor. What it could not say is that a model wrote the
interpretation, which harness it ran inside, or that a second person was in the session -- and
``producer`` answered only part of that, only for derivations, and only in a shape that made a
version string load-bearing.

These tests pin the two things that can go wrong. Selection: a record that names nobody must keep
the bytes it had before schema version 2 existed, or every brain re-versions itself for nothing.
And matching: a brain holds records of both versions at once, so a batch invalidation that read
only one shape would silently miss blocks.
"""

import pytest
from pydantic import ValidationError as PydanticValidationError

from boltzmann.blocks.base import Block
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    Collaborator,
    DemotionRecord,
    DerivationRecord,
    Producer,
    ProducerKind,
    ProvenanceBlock,
    ProvenanceBlockV2,
    RegistrationRecord,
    RemovalMechanism,
    RemovalRecord,
    provenance_block,
)
from boltzmann.exceptions import ActorIdError
from boltzmann.identity.digest import BlockId
from boltzmann.module.ledger import Ledger

ALEX = Actor(id="alex@example.org", kind=ActorKind.HUMAN)
JUAN = Collaborator(id="juan@example.org", kind=ActorKind.HUMAN)
CLAUDE_CODE = Collaborator(id="anthropic/claude-code", kind=ActorKind.AGENT, model="anthropic/fable-5")
CODEX = Collaborator(id="openai/codex", kind=ActorKind.AGENT, model="openai/gpt-5.6-sol")
HERMES = Collaborator(id="nousresearch/hermes", kind=ActorKind.AGENT)
SOLO = Producer(kind=ProducerKind.ACTOR, id="alex@example.org")

BLOCK = BlockId.of(b"a derived block")
SOURCE = BlockId.of(b"the evidence it cites")
AT = "2026-08-31T00:00:00Z"


def derivation(*, block: BlockId = BLOCK, producer: Producer = SOLO) -> DerivationRecord:
    return DerivationRecord(block=block, derived_from=[SOURCE], actor=ALEX, at=AT, producer=producer)


class TestWhichVersionARecordLandsOn:
    def test_a_person_working_alone_stays_at_version_one(self) -> None:
        """The case that must not regress. If naming nobody re-versioned a record, every brain
        would become unreadable to older clients to record knowledge they could have read."""
        block = provenance_block(derivation())

        assert isinstance(block, ProvenanceBlock)
        assert block.SCHEMA_VERSION == 1

    def test_naming_who_assisted_takes_version_two(self) -> None:
        block = provenance_block(derivation(), [CLAUDE_CODE])

        assert isinstance(block, ProvenanceBlockV2)
        assert block.SCHEMA_VERSION == 2
        assert block.record.assisted_by == [CLAUDE_CODE]

    def test_a_version_one_derivation_keeps_the_identity_it_always_had(self) -> None:
        """Byte-for-byte, not merely 'still valid'. The block_id is what other brains reference."""
        before = ProvenanceBlock(record=derivation())

        assert provenance_block(derivation()).block_id == before.block_id
        assert provenance_block(derivation()).canonical_bytes() == before.canonical_bytes()

    def test_version_two_drops_the_producer_rather_than_carrying_both(self) -> None:
        """The two versions are disjoint, which is what makes selection unambiguous without
        consulting anything outside the payload."""
        block = provenance_block(derivation(), [CLAUDE_CODE])

        assert not hasattr(block.record, "producer")

    @pytest.mark.parametrize(
        "record",
        [
            RegistrationRecord(block=BLOCK, actor=ALEX, at=AT),
            DemotionRecord(block=BLOCK, actor=ALEX, at=AT),
        ],
        ids=["registration", "demotion"],
    )
    def test_a_record_that_never_had_a_producer_simply_gains_the_parties(self, record) -> None:
        assert provenance_block(record).SCHEMA_VERSION == 1
        assert provenance_block(record, [CODEX]).SCHEMA_VERSION == 2

    def test_a_derivation_carrying_both_or_neither_satisfies_no_schema(self) -> None:
        """Version 1 obliged a writer to say what produced a derived block. Dropping ``producer``
        must not quietly relax that into "derived, and I decline to say by what"."""
        base = {
            "record_type": "derivation",
            "block": str(BLOCK),
            "derived_from": [str(SOURCE)],
            "actor": ALEX.model_dump(mode="json", exclude_none=True),
            "at": AT,
        }
        producer = SOLO.model_dump(mode="json", exclude_none=True)
        parties = [CLAUDE_CODE.model_dump(mode="json", exclude_none=True)]

        for payload in ({**base, "producer": producer, "assisted_by": parties}, base):
            with pytest.raises(PydanticValidationError):
                Block.build(MemoryType.PROVENANCE, {"record": payload})


class TestWhatACollaboratorMayBe:
    def test_a_runtime_and_the_model_it_ran_stay_one_entry(self) -> None:
        """The same model under a different harness is a different collaborator: the harness decides
        what the model sees, how many turns it gets, and which tools it can reach. Splitting them
        into two entries would lose which ran where the moment a second agent appears."""
        block = provenance_block(derivation(), [CLAUDE_CODE, CODEX])
        parties = {party.id: party.model for party in block.record.assisted_by}

        assert parties == {
            "anthropic/claude-code": "anthropic/fable-5",
            "openai/codex": "openai/gpt-5.6-sol",
        }

    def test_a_harness_may_decline_to_name_its_model(self) -> None:
        """A smaller claim than naming a model that was guessed."""
        block = provenance_block(derivation(), [HERMES])

        assert block.record.assisted_by[0].model is None

    def test_people_and_agents_share_one_shape(self) -> None:
        """So that reading "who took part" never branches."""
        block = provenance_block(derivation(), [CODEX, JUAN])

        assert [party.kind for party in block.record.assisted_by] == [ActorKind.AGENT, ActorKind.HUMAN]

    def test_a_person_may_not_name_a_model(self) -> None:
        """Left unchecked it reads as "this person is a model" to everything that groups by model,
        including a batch invalidation, which would then reach a person's work."""
        with pytest.raises(ValueError, match="does not run a model"):
            Collaborator(id="juan@example.org", kind=ActorKind.HUMAN, model="anthropic/fable-5")

    def test_an_identifier_inside_a_version_two_record_must_resolve(self) -> None:
        """Strict here and permissive on ``Actor`` itself: version 2 is new, so nothing is stranded
        by enforcing it, and a legacy identifier reaching this far means a writer built a new record
        out of an old habit."""
        with pytest.raises(ActorIdError, match="assisting party id"):
            provenance_block(derivation(), [Collaborator(id="codex", kind=ActorKind.AGENT)])

        with pytest.raises(ActorIdError, match="model id"):
            provenance_block(derivation(), [Collaborator(id="openai/codex", kind=ActorKind.AGENT, model="gpt5")])


class TestTheRemovalLedgerStaysReadable:
    def test_a_removal_never_leaves_version_one(self) -> None:
        """See ``ProvenanceEntryV2``. A removal record is the one a verifier must *decode* to decide
        a blocking question, so a version an older client lacks turns "I cannot read this" into "you
        violated the removal invariant" -- confident, specific, wrong, and unwithdrawable."""
        removal = RemovalRecord(
            blocks=[BLOCK],
            mechanism=RemovalMechanism.DROP,
            memory_type=MemoryType.SEMANTIC,
            actor=ALEX,
            at=AT,
            reason="ingested in error",
        )

        assisted = provenance_block(removal, [CLAUDE_CODE])

        assert assisted.SCHEMA_VERSION == 1
        assert assisted.block_id == provenance_block(removal).block_id

    def test_the_removal_still_records_who_is_answerable(self) -> None:
        """What it gives up is naming who assisted, not who did it."""
        removal = RemovalRecord(
            blocks=[BLOCK],
            mechanism=RemovalMechanism.DROP,
            memory_type=MemoryType.SEMANTIC,
            actor=ALEX,
            at=AT,
            reason="ingested in error",
        )

        assert provenance_block(removal, [CLAUDE_CODE]).record.actor == ALEX


class TestBatchInvalidationAcrossBothShapes:
    """A brain holds records of both versions at once, so one query has two places to look."""

    def ledger(self) -> Ledger:
        ledger = Ledger()
        old = provenance_block(
            derivation(producer=Producer(kind=ProducerKind.MODEL, id="anthropic/fable-5", version="2026-06"))
        )
        ledger._absorb(old, old.block_id)
        new_block = BlockId.of(b"a block derived at version two")
        new = provenance_block(derivation(block=new_block), [CLAUDE_CODE, JUAN])
        ledger._absorb(new, new.block_id)
        return ledger

    def test_one_model_query_reaches_both_record_shapes(self) -> None:
        """A batch invalidation that read only one shape would silently miss blocks, which is a
        worse failure than reaching further than strictly necessary."""
        found = self.ledger().made_by(Producer(kind=ProducerKind.MODEL, id="anthropic/fable-5"))

        assert found == {BLOCK, BlockId.of(b"a block derived at version two")}

    def test_a_person_is_never_matched_as_a_model(self) -> None:
        """A human collaborator carries no model at all, so asking for one cannot reach their work."""
        assert self.ledger().made_by(Producer(kind=ProducerKind.MODEL, id="juan@example.org")) == set()

    def test_a_runtime_is_reachable_by_its_own_identifier(self) -> None:
        """ "Everything that went through this harness" is a question worth being able to ask, and
        version 1 could not express it at all."""
        found = self.ledger().made_by(Producer(kind=ProducerKind.PIPELINE, id="anthropic/claude-code"))

        assert found == {BlockId.of(b"a block derived at version two")}

    def test_a_version_given_in_the_query_still_narrows_version_one_records(self) -> None:
        """The granularity is not withdrawn from records that have it, only from ones that never
        carried a version to narrow by.

        The version-1 record matches, because it names that version. The version-2 record also
        matches, because it names the model and has no version to be excluded by -- which is the
        trade the protocol makes for an identifier nobody has to invent.
        """
        ledger = self.ledger()

        stale = Producer(kind=ProducerKind.MODEL, id="anthropic/fable-5", version="2025-01")

        assert ledger.made_by(stale) == {BlockId.of(b"a block derived at version two")}
