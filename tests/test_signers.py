"""The ssh-agent seam: the SDK signs without ever holding a key, against a real socket.

The fake agent here is not a mock of our own client -- it is an independent implementation of
the agent protocol's server side over a real AF_UNIX socket, so the wire codec is exercised from
the other end: framing bugs that a mock would mirror show up here as protocol failures.
"""

from __future__ import annotations

import socketserver
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from boltzmann.authenticity import (
    AgentSigner,
    SshAgentClient,
    SshPublicKey,
    SshSignature,
    WireReader,
    put_string,
    put_uint32,
    sign,
    verify,
)
from boltzmann.authenticity.agent import (
    SSH_AGENT_FAILURE,
    SSH_AGENT_IDENTITIES_ANSWER,
    SSH_AGENT_SIGN_RESPONSE,
    SSH_AGENTC_REQUEST_IDENTITIES,
    SSH_AGENTC_SIGN_REQUEST,
)
from boltzmann.exceptions import SignerUnavailableError, UnsupportedKeyTypeError

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")

MESSAGE = b'{"boltzmann":1}'


def keypair(seed: int) -> tuple[SshPublicKey, object]:
    private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    line = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return SshPublicKey.parse(line.decode("ascii")), private


class FakeAgent:
    """An ssh-agent server over a real unix socket, backed by in-memory Ed25519 keys."""

    def __init__(self, held: dict[bytes, object]) -> None:
        self.held = held
        # The socket path must stay under the AF_UNIX limit (~104 bytes on macOS), which
        # pytest's deeply nested tmp_path does not guarantee -- so a short mkdtemp instead.
        self._dir = tempfile.mkdtemp(prefix="agent-")
        self.path = str(Path(self._dir) / "sock")
        held_keys = self.held

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                while True:
                    prefix = self.rfile.read(4)
                    if len(prefix) < 4:
                        return
                    length = WireReader(prefix).uint32()
                    payload = self.rfile.read(length)
                    kind = payload[0]
                    if kind == SSH_AGENTC_REQUEST_IDENTITIES:
                        body = bytes([SSH_AGENT_IDENTITIES_ANSWER]) + put_uint32(len(held_keys))
                        for blob in held_keys:
                            body += put_string(blob) + put_string(b"test key")
                    elif kind == SSH_AGENTC_SIGN_REQUEST:
                        reader = WireReader(payload[1:])
                        blob = reader.string()
                        data = reader.string()
                        reader.uint32()
                        private = held_keys.get(blob)
                        if private is None:
                            body = bytes([SSH_AGENT_FAILURE])
                        else:
                            raw = private.sign(data)  # type: ignore[attr-defined]
                            body = bytes([SSH_AGENT_SIGN_RESPONSE]) + put_string(
                                put_string(b"ssh-ed25519") + put_string(raw)
                            )
                    else:
                        body = bytes([SSH_AGENT_FAILURE])
                    self.wfile.write(put_uint32(len(body)) + body)

        self.server = socketserver.ThreadingUnixStreamServer(self.path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def agent() -> Iterator[tuple[FakeAgent, SshPublicKey]]:
    public, private = keypair(0x42)
    fake = FakeAgent({public.blob: private})
    yield fake, public
    fake.close()


class TestAgentClient:
    """The raw protocol: framing, identities, signatures, refusals."""

    def test_identities_lists_the_held_key(self, agent: tuple[FakeAgent, SshPublicKey]) -> None:
        fake, public = agent
        with SshAgentClient(socket_path=fake.path) as client:
            held = client.request_identities()
        assert [identity.fingerprint for identity, _ in held] == [public.fingerprint]

    def test_the_agents_signature_is_an_rfc4253_blob_that_drops_into_sshsig(
        self, agent: tuple[FakeAgent, SshPublicKey]
    ) -> None:
        fake, public = agent
        signer = AgentSigner(public, socket_path=fake.path)
        signature = sign(MESSAGE, signer)
        assert verify(signature, MESSAGE).matches(public)
        assert SshSignature.parse(signature.armored()) == signature

    def test_a_key_the_agent_does_not_hold_is_a_refusal(self, agent: tuple[FakeAgent, SshPublicKey]) -> None:
        fake, _ = agent
        stranger, _ = keypair(0x43)
        with SshAgentClient(socket_path=fake.path) as client, pytest.raises(SignerUnavailableError, match="refused"):
            client.sign(stranger, MESSAGE)

    def test_no_agent_at_all_is_a_typed_failure(self) -> None:
        with pytest.raises(SignerUnavailableError):
            SshAgentClient(socket_path="/nonexistent/agent.sock")

    def test_an_unset_environment_is_named_not_guessed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        with pytest.raises(SignerUnavailableError, match="SSH_AUTH_SOCK"):
            SshAgentClient()


class TestAgentSigner:
    """Key selection happens up front, before any hardware token is asked for a touch."""

    def test_selects_by_fingerprint(self, agent: tuple[FakeAgent, SshPublicKey]) -> None:
        fake, public = agent
        signer = AgentSigner(public.fingerprint, socket_path=fake.path)
        assert signer.public_key.matches(public)

    def test_selects_the_only_ed25519_key_when_unnamed(self, agent: tuple[FakeAgent, SshPublicKey]) -> None:
        fake, public = agent
        assert AgentSigner(socket_path=fake.path).public_key.matches(public)

    def test_an_ambiguous_choice_is_refused_not_guessed(self) -> None:
        first, private_first = keypair(0x44)
        second, private_second = keypair(0x45)
        fake = FakeAgent({first.blob: private_first, second.blob: private_second})
        try:
            with pytest.raises(SignerUnavailableError, match="name the one"):
                AgentSigner(socket_path=fake.path)
        finally:
            fake.close()

    def test_a_missing_key_is_a_refusal_naming_the_key(self, agent: tuple[FakeAgent, SshPublicKey]) -> None:
        fake, _ = agent
        stranger, _ = keypair(0x46)
        with pytest.raises(SignerUnavailableError, match="does not hold"):
            AgentSigner(stranger, socket_path=fake.path)

    def test_a_non_ed25519_key_is_refused_before_any_touch(self) -> None:
        public, private = keypair(0x47)
        rsa_blob = put_string(b"ssh-rsa") + put_string(b"\x01\x00\x01") + put_string(b"\x00" * 64)
        fake = FakeAgent({rsa_blob: private, public.blob: private})
        try:
            with pytest.raises(UnsupportedKeyTypeError, match="cannot verify"):
                AgentSigner(SshPublicKey.from_blob(rsa_blob), socket_path=fake.path)
        finally:
            fake.close()

    def test_identities_classmethod_lists_what_is_held(self, agent: tuple[FakeAgent, SshPublicKey]) -> None:
        fake, public = agent
        held = AgentSigner.identities(socket_path=fake.path)
        assert [identity.fingerprint for identity in held] == [public.fingerprint]
