"""An actor identifier is hashed into a block, so it has to have a form.

A provenance record is a block. The actor's identifier enters the payload, the payload enters the
envelope, and the envelope is what ``block_id`` is computed over -- so two spellings of one person
are two names for one fact, and neither party fails. That is the same silent divergence canonical
serialization exists to prevent, arriving through a field nobody had canonicalized.

These tests pin the grammar and, more importantly, pin the *asymmetry*: strict where an identifier
is chosen, permissive where one is read, because a validator on the type itself would make every
brain written before this rule unreadable.
"""

import pytest

from boltzmann.blocks.provenance import Actor, ActorKind, ProvenanceBlock, RegistrationRecord
from boltzmann.brain import Brain
from boltzmann.exceptions import ActorIdError
from boltzmann.identity.digest import BlockId
from boltzmann.identity.principal import ActorIdForm, actor_id_form, is_actor_id, parse_actor_id
from boltzmann.ingest.register import RegistrationRequest
from boltzmann.store.memory import MemoryBlockStore

CURATOR = Actor(id="curator@example.org", kind=ActorKind.HUMAN)


class TestTheTwoAcceptedForms:
    @pytest.mark.parametrize(
        "value",
        [
            "alex@alquimia.ai",
            "a@b.co",
            "curator@example.org",
            "first.last@sub.example.org",
            "alex+notes@example.org",
        ],
    )
    def test_an_address_is_accepted(self, value: str) -> None:
        assert actor_id_form(value) is ActorIdForm.ADDRESS

    @pytest.mark.parametrize(
        "value",
        [
            "github.com/alexfiorenza",
            "anthropic/claude-code",
            "anthropic/fable-5",
            "openai/gpt-5.6-sol",
            "gaussia/nightly-ingest",
        ],
    )
    def test_a_namespaced_name_is_accepted(self, value: str) -> None:
        assert actor_id_form(value) is ActorIdForm.NAMESPACED

    def test_the_form_does_not_depend_on_what_the_party_is(self) -> None:
        """A model, a runtime and a pipeline share one grammar on purpose.

        What a party *is* travels beside the identifier as its kind. Encoding it into the
        identifier instead would make a reclassification a rename, and a rename changes every
        ``block_id`` that named it.
        """
        assert actor_id_form("anthropic/fable-5") is actor_id_form("gaussia/nightly-ingest")


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        ("value", "because"),
        [
            ("curator", "neither an address nor a namespaced name"),
            ("sam", "neither an address nor a namespaced name"),
            ("", "empty"),
            ("Alex@alquimia.ai", "not lowercase"),
            ("alex@Alquimia.ai", "not lowercase"),
            ("Anthropic/claude-code", "not lowercase"),
            ("mailto:alex@alquimia.ai", "does not admit"),
            ("https://github.com/alexfiorenza", "more than one '/'"),
            ("anthropic/models/fable-5", "more than one '/'"),
            ("/claude-code", "namespace is empty"),
            ("anthropic/", "name is empty"),
            ("alex@", "domain is empty"),
            ("@alquimia.ai", "local part is empty"),
            ("alex@localhost", "one label"),
            ("alex f@alquimia.ai", "whitespace"),
            ("alex@alquimia.ai/notes", "both '@' and '/'"),
            ("alex..f@alquimia.ai", "consecutive dots"),
            ("anthropic/-claude-code", "bounded by alphanumerics"),
        ],
    )
    def test_each_refusal_says_which_rule_it_broke(self, value: str, because: str) -> None:
        """A caller who cannot tell what is wrong works around the check instead of fixing the value."""
        assert not is_actor_id(value)
        with pytest.raises(ActorIdError, match=because):
            parse_actor_id(value)

    def test_an_identifier_is_refused_rather_than_normalized(self) -> None:
        """Lowering it would mint a ``block_id`` the caller neither asked for nor can predict --
        and therefore one they cannot search for either."""
        with pytest.raises(ActorIdError):
            parse_actor_id("Alex@Example.org")

    def test_a_long_identifier_is_bounded(self) -> None:
        """The value arrives inside an artifact, so its size is chosen by whoever wrote the artifact."""
        with pytest.raises(ActorIdError, match="over the 320"):
            parse_actor_id("a" * 320 + "@example.org")

    def test_the_field_name_travels_into_the_message(self) -> None:
        """An error about an assisting party's model must not read as one about the actor."""
        with pytest.raises(ActorIdError, match="model id"):
            parse_actor_id("fable-5", field="model id")


class TestWhereItIsEnforced:
    def test_a_brain_refuses_to_open_under_an_unusable_identifier(self) -> None:
        with pytest.raises(ActorIdError):
            Brain(MemoryBlockStore(), Actor(id="curator", kind=ActorKind.HUMAN))

    def test_a_registration_request_refuses_one_too(self) -> None:
        with pytest.raises(ActorIdError, match="not usable"):
            RegistrationRequest(media_type="text/plain", actor=Actor(id="curator", kind=ActorKind.HUMAN))

    def test_the_actor_model_itself_stays_permissive(self) -> None:
        """The asymmetry is the whole design, and this is the half that is easy to lose.

        Every provenance record ever written decodes through ``Actor``. A validator on the type
        would make every brain that predates this rule unreadable, which punishes readers for a
        writer's old habit -- and the writer is already gone.
        """
        legacy = Actor(id="curator", kind=ActorKind.HUMAN)
        assert legacy.id == "curator"

    def test_a_record_written_before_the_rule_still_decodes(self) -> None:
        """The bytes are already published. Refusing them now would strand the brains that hold them."""
        block = ProvenanceBlock(
            record=RegistrationRecord(
                block=BlockId.of(b"a source"),
                actor=Actor(id="curator", kind=ActorKind.HUMAN),
                at="2026-01-01T00:00:00Z",
            )
        )
        store = MemoryBlockStore()
        store.put_block(block)

        decoded = store.get_block(block.block_id)

        assert decoded.block_id == block.block_id
        assert decoded.record.actor.id == "curator"

    def test_a_brain_opens_under_an_identifier_that_resolves(self) -> None:
        brain = Brain(MemoryBlockStore(), CURATOR)
        assert brain.actor.id == "curator@example.org"
