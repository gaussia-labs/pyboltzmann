"""The ssh-agent client: how this SDK signs without ever touching a private key.

The private key never enters the protocol (paper Section 8.3): it stays where SSH keys already
live -- an agent, a hardware token behind an agent, a KMS shim that presents as one. This client
speaks the ssh-agent protocol (RFC 9987) over the socket ``SSH_AUTH_SOCK`` names, asking for
exactly two things: which keys are held, and a signature over bytes it supplies.

The agent knows nothing about SSHSIG. It returns an RFC 4253 signature blob -- ``string
algorithm || string raw`` -- which happens to be exactly the shape SSHSIG's ``signature`` field
wants, so the blob drops in verbatim. But the framing of what gets *signed* is entirely the
caller's: hand the agent a raw message instead of the signed-data blob and you get a signature
over the wrong bytes that verifies against nothing.
"""

from __future__ import annotations

import os
import socket
import sys
from types import TracebackType
from typing import Self

from boltzmann.authenticity.keys import SshPublicKey
from boltzmann.authenticity.wire import WireReader, put_string, put_uint32
from boltzmann.exceptions import SignerUnavailableError

SSH_AGENT_FAILURE = 5
SSH_AGENTC_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12
SSH_AGENTC_SIGN_REQUEST = 13
SSH_AGENT_SIGN_RESPONSE = 14

AGENT_MAX_MESSAGE = 256 * 1024
"""Longest agent response accepted. An identities answer for hundreds of keys fits easily."""

WINDOWS_PIPE = r"\\.\pipe\openssh-ssh-agent"
"""Where Windows OpenSSH serves the same protocol: a named pipe, with no environment variable."""


class SshAgentClient:
    """
    A connection to one ssh-agent.

    Attributes:
        socket_path (str | None): Where the agent listens. Defaults to ``SSH_AUTH_SOCK`` on
            POSIX and the OpenSSH named pipe on Windows.
        timeout (float): Seconds to wait on the agent before giving up.
    """

    def __init__(self, socket_path: str | None = None, timeout: float = 30.0) -> None:
        """
        Connect to the agent.

        Args:
            socket_path (str | None): Explicit socket path or pipe name.
            timeout (float): Seconds before a silent agent is a failure.

        Raises:
            SignerUnavailableError: If no agent is listening -- ``SSH_AUTH_SOCK`` unset, the
                socket refused, or the pipe absent.
        """
        self.timeout = timeout
        if socket_path is None and sys.platform != "win32":
            socket_path = os.environ.get("SSH_AUTH_SOCK")
            if not socket_path:
                raise SignerUnavailableError(
                    "SSH_AUTH_SOCK is not set: no ssh-agent is advertised in this environment. "
                    "Start one (or forward one), or sign from a machine that has one."
                )
        self.socket_path = socket_path
        self._channel = self._connect()

    def _connect(self):
        if sys.platform == "win32":  # pragma: no cover - exercised only on Windows
            pipe = self.socket_path or WINDOWS_PIPE
            try:
                return open(pipe, "r+b", buffering=0)  # noqa: PTH123 - a pipe, not a file to manage
            except OSError as error:
                raise SignerUnavailableError(f"no ssh-agent pipe at {pipe}: {error}") from error
        if self.socket_path is None:  # pragma: no cover - unreachable: __init__ resolved or raised
            raise SignerUnavailableError("no ssh-agent socket path could be resolved")
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        channel.settimeout(self.timeout)
        try:
            channel.connect(self.socket_path)
        except OSError as error:
            channel.close()
            raise SignerUnavailableError(f"could not reach the ssh-agent at {self.socket_path}: {error}") from error
        return channel

    # --- Context management -----------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection. Idempotent."""
        self._channel.close()

    # --- Framing ------------------------------------------------------------------

    def _send(self, payload: bytes) -> None:
        self._channel.sendall(put_uint32(len(payload)) + payload)

    def _receive_exactly(self, count: int) -> bytes:
        chunks: list[bytes] = []
        remaining = count
        while remaining:
            chunk = self._channel.recv(remaining)
            if not chunk:
                raise SignerUnavailableError("the ssh-agent closed the connection mid-reply")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _roundtrip(self, payload: bytes) -> WireReader:
        try:
            self._send(payload)
            declared = WireReader(self._receive_exactly(4)).uint32()
            if declared > AGENT_MAX_MESSAGE:
                raise SignerUnavailableError(
                    f"the ssh-agent declared a {declared}-byte reply, over the {AGENT_MAX_MESSAGE} cap"
                )
            return WireReader(self._receive_exactly(declared), max_string=AGENT_MAX_MESSAGE)
        except TimeoutError as error:
            raise SignerUnavailableError(
                f"the ssh-agent did not answer within {self.timeout}s; a hardware token may be "
                f"waiting for a touch that never came"
            ) from error

    if sys.platform == "win32":  # pragma: no cover - Windows pipes read differently

        def _receive_exactly(self, count: int) -> bytes:
            data = self._channel.read(count)
            if data is None or len(data) < count:
                raise SignerUnavailableError("the ssh-agent pipe closed mid-reply")
            return data

    # --- The two requests -----------------------------------------------------------

    def request_identities(self) -> list[tuple[SshPublicKey, str]]:
        """
        The keys the agent holds, with their comments.

        Returns:
            list[tuple[SshPublicKey, str]]: Each held key and the comment the agent stores for
            it. Keys whose blobs do not parse are skipped rather than fatal: an agent may hold
            key types this SDK has never heard of, and listing is not endorsing.

        Raises:
            SignerUnavailableError: If the agent refuses or the reply is malformed.
        """
        reply = self._roundtrip(bytes([SSH_AGENTC_REQUEST_IDENTITIES]))
        kind = reply.fixed(1)[0]
        if kind != SSH_AGENT_IDENTITIES_ANSWER:
            raise SignerUnavailableError(f"the ssh-agent answered message type {kind} to an identities request")
        count = reply.uint32()
        identities: list[tuple[SshPublicKey, str]] = []
        for _ in range(count):
            blob = reply.string()
            comment = reply.string().decode("utf-8", errors="replace")
            try:
                identities.append((SshPublicKey.from_blob(blob), comment))
            except Exception:
                continue
        return identities

    def sign(self, key: SshPublicKey, data: bytes, flags: int = 0) -> bytes:
        """
        Ask the agent to sign ``data`` with a held key.

        Args:
            key (SshPublicKey): Which key, selected by its exact blob.
            data (bytes): The exact bytes to sign -- for SSHSIG, the signed-data blob, already
                framed by the caller.
            flags (int): Agent signing flags. ``0`` for Ed25519; the RSA ``SHA2`` flags are the
                only defined ones and irrelevant here.

        Returns:
            bytes: The RFC 4253 signature blob, ready for SSHSIG's ``signature`` field verbatim.

        Raises:
            SignerUnavailableError: If the agent refuses -- most commonly because it does not
                hold the key -- or replies with anything but a signature.
        """
        request = bytes([SSH_AGENTC_SIGN_REQUEST]) + put_string(key.blob) + put_string(data) + put_uint32(flags)
        reply = self._roundtrip(request)
        kind = reply.fixed(1)[0]
        if kind == SSH_AGENT_FAILURE:
            raise SignerUnavailableError(
                f"the ssh-agent refused to sign with {key.fingerprint}; it most likely does not "
                f"hold that key, or a confirmation was declined"
            )
        if kind != SSH_AGENT_SIGN_RESPONSE:
            raise SignerUnavailableError(f"the ssh-agent answered message type {kind} to a sign request")
        signature = reply.string()
        reply.finish()
        return signature
