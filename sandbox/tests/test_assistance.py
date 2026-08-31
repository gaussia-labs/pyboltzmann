"""The sandbox is the one place an agent actually calls in, so it is where this has to be true.

Before this, every write here claimed ``ActorKind.HUMAN`` -- including the MCP server paths, where
the caller is by construction an agent. The record said a person did work a model did, which is the
one thing an audit trail must not do quietly.

These tests run against the settings rather than the transport, because that is where the decision
is made: the operator says who is driving, and everything downstream records it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind
from boltzmann.ingest.register import RegistrationRequest

from boltzmann_sandbox.brain import open_brain
from boltzmann_sandbox.config import Settings

HUMAN = "alex@example.org"
RUNTIME = "anthropic/claude-code"
MODEL = "anthropic/fable-5"


def settings(tmp_path: Path, *, agent: str = "", model: str = "") -> Settings:
    return Settings(
        registry="localhost:5000/test/brain",
        configured="localhost:5000/test/brain",
        tag="latest",
        brain_path=tmp_path / "brain",
        actor=HUMAN,
        agent=agent,
        agent_model=model,
        username="",
        token="",
        anonymous=True,
        insecure=True,
    )


def registration_record(brain) -> object:
    module = brain.module(MemoryType.PROVENANCE)
    for block_id in module.block_ids:
        block = module.get(block_id)
        if block.record.record_type == "registration":
            return block
    raise AssertionError("no registration record was written")


class TestWhatTheSandboxRecords:
    def test_an_agent_driven_write_names_the_human_and_the_agent(self, tmp_path: Path) -> None:
        """The point of the whole exercise. The actor is whose account the work runs under; the
        agent is what did it. Recording only one of them loses the half a reader came for."""
        brain = open_brain(settings(tmp_path, agent=RUNTIME, model=MODEL))
        brain.register(
            b"a source about signals",
            RegistrationRequest(media_type="text/plain", actor=Actor(id=HUMAN, kind=ActorKind.HUMAN)),
        )

        record = registration_record(brain).record

        assert record.actor.id == HUMAN
        assert [(p.id, p.model) for p in record.assisted_by] == [(RUNTIME, MODEL)]

    def test_a_person_working_alone_records_nobody_and_stays_at_version_one(self, tmp_path: Path) -> None:
        """The regression that would cost the most and show the least: if the sandbox always named
        an agent, every brain it wrote would leave schema version 1 for nothing."""
        brain = open_brain(settings(tmp_path))
        brain.register(
            b"a source about signals",
            RegistrationRequest(media_type="text/plain", actor=Actor(id=HUMAN, kind=ActorKind.HUMAN)),
        )

        block = registration_record(brain)

        assert block.SCHEMA_VERSION == 1
        assert not hasattr(block.record, "assisted_by")

    def test_an_agent_whose_model_is_unknown_says_so_rather_than_guessing(self, tmp_path: Path) -> None:
        brain = open_brain(settings(tmp_path, agent=RUNTIME))
        brain.register(
            b"a source about signals",
            RegistrationRequest(media_type="text/plain", actor=Actor(id=HUMAN, kind=ActorKind.HUMAN)),
        )

        [party] = registration_record(brain).record.assisted_by

        assert party.id == RUNTIME
        assert party.model is None


class TestTheConfiguration:
    def test_no_agent_configured_means_no_assisting_parties(self, tmp_path: Path) -> None:
        assert settings(tmp_path).assisting == []

    def test_the_runtime_and_its_model_stay_one_entry(self, tmp_path: Path) -> None:
        """Two entries would lose which model ran where the moment a second agent appears."""
        [party] = settings(tmp_path, agent=RUNTIME, model=MODEL).assisting

        assert (party.id, party.model, party.kind) == (RUNTIME, MODEL, ActorKind.AGENT)

    def test_a_login_name_is_not_accepted_as_an_actor(self, tmp_path: Path) -> None:
        """``$USER`` resolves on one machine and nowhere else, which is why the fallback namespaces
        it. A brain opened under a bare login name would name a person nobody else can identify."""
        from boltzmann.exceptions import ActorIdError

        bare = settings(tmp_path)
        object.__setattr__(bare, "actor", "alex")

        with pytest.raises(ActorIdError):
            open_brain(bare)

    def test_the_default_actor_is_namespaced_rather_than_bare(self) -> None:
        from boltzmann.identity.principal import is_actor_id

        from boltzmann_sandbox.config import _sandbox_actor

        previous = os.environ.get("USER")
        os.environ["USER"] = "alex"
        try:
            assert _sandbox_actor() == "sandbox/alex"
            assert is_actor_id(_sandbox_actor())
        finally:
            if previous is None:
                del os.environ["USER"]
            else:
                os.environ["USER"] = previous
