"""The signing seam: who can produce a signature, without the SDK ever holding a key.

The private key never enters the protocol (paper Section 8.3): it stays where SSH keys already
live -- an agent, a hardware token, a key-management service. A conforming implementation MUST
NOT require a private key to be stored inside a brain and MUST NOT define a format for one. This
module therefore defines the *seam* only; the one backend the SDK ships speaks to an ssh-agent
(:mod:`boltzmann.authenticity.agent`), where the key material is someone else's problem.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from boltzmann.authenticity.keys import SshPublicKey


@runtime_checkable
class Signer(Protocol):
    """
    Produces an SSH signature over already-framed bytes.

    A signer receives the SSHSIG *signed-data blob*, not the message. The SSHSIG framing is the
    protocol's, and a backend that reconstructed it would be a second implementation of the
    format to keep in step -- so the framing happens exactly once, in
    :func:`boltzmann.authenticity.sshsig.sign`, and every backend signs what it is given.
    """

    @property
    def public_key(self) -> SshPublicKey:
        """The public half of the key this signer signs with."""
        ...

    def sign_blob(self, data: bytes) -> bytes:
        """
        Sign framed bytes and return an RFC 4253 signature blob.

        Args:
            data (bytes): The exact bytes to sign -- for SSHSIG, the signed-data blob.

        Returns:
            bytes: The signature as ``string algorithm || string raw`` -- the form an ssh-agent
            returns and the form SSHSIG's ``signature`` field carries, so no re-framing happens
            anywhere.

        Raises:
            SignerUnavailableError: If the backend cannot sign -- no agent, a refusal, or a key
                it does not hold.
        """
        ...


class AgentSigner:
    """
    The signer the SDK ships: keys stay in an ssh-agent, hardware tokens included.

    Attributes:
        public_key (SshPublicKey): The key this signer signs with, confirmed held by the agent
            at construction.
        comment (str): The comment the agent stores for the key, kept rather than discarded. It is
            conventionally an address or ``user@host``, which is exactly the shape a trust root's
            ``subject`` takes -- see :meth:`suggested_subject`.
        socket_path (str | None): Where the agent listens, when not the environment's default.
        timeout (float): Seconds to wait on the agent -- generous by default, because a hardware
            token waits for a human touch.
    """

    def __init__(
        self,
        key: SshPublicKey | str | None = None,
        socket_path: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        """
        Bind a signer to one agent-held key.

        Args:
            key (SshPublicKey | str | None): Which key -- an :class:`SshPublicKey`, a
                ``SHA256:`` fingerprint, or an authorized_keys line. ``None`` selects the
                agent's only Ed25519 key, and is an error when there is more than one: guessing
                among identities is how the wrong key signs.
            socket_path (str | None): Explicit agent socket or pipe.
            timeout (float): Seconds before a silent agent is a failure.

        Raises:
            SignerUnavailableError: If no agent is reachable, the key is not held, or ``None``
                was given and the choice is ambiguous.
            UnsupportedKeyTypeError: If the selected key is not Ed25519 -- detected *before* any
                signing, so a hardware token is never asked for a touch this SDK cannot verify.
        """
        from boltzmann.authenticity.agent import SshAgentClient
        from boltzmann.exceptions import SignerUnavailableError, UnsupportedKeyTypeError

        self.socket_path = socket_path
        self.timeout = timeout
        with SshAgentClient(socket_path=socket_path, timeout=timeout) as agent:
            identities = agent.request_identities()
        held = [identity for identity, _ in identities]
        comments = {identity.fingerprint: comment for identity, comment in identities}
        selected: SshPublicKey | None = None
        if key is None:
            candidates = [identity for identity in held if identity.is_ed25519]
            if len(candidates) != 1:
                listing = ", ".join(identity.fingerprint for identity in candidates) or "none"
                raise SignerUnavailableError(
                    f"the agent holds {len(candidates)} Ed25519 keys ({listing}); name the one to "
                    f"sign with rather than having it guessed"
                )
            selected = candidates[0]
        elif isinstance(key, str) and key.startswith("SHA256:"):
            for identity in held:
                if identity.fingerprint == key:
                    selected = identity
                    break
        else:
            wanted = SshPublicKey.parse(key)
            for identity in held:
                if identity.matches(wanted):
                    selected = identity
                    break
        if selected is None:
            raise SignerUnavailableError(f"the ssh-agent does not hold {key!r}; ssh-add it, or point at another agent")
        if not selected.is_ed25519:
            raise UnsupportedKeyTypeError(
                f"{selected.fingerprint} is {selected.key_type}, which this SDK cannot verify; "
                f"signing with it would produce signatures no conforming consumer here accepts"
            )
        self.public_key = selected
        self.comment = comments.get(selected.fingerprint, "")

    @classmethod
    def identities(cls, socket_path: str | None = None, timeout: float = 30.0) -> list[SshPublicKey]:
        """
        The keys an agent holds, for choosing one.

        Args:
            socket_path (str | None): Explicit agent socket or pipe.
            timeout (float): Seconds before a silent agent is a failure.

        Returns:
            list[SshPublicKey]: The held keys.
        """
        from boltzmann.authenticity.agent import SshAgentClient

        with SshAgentClient(socket_path=socket_path, timeout=timeout) as agent:
            return [identity for identity, _ in agent.request_identities()]

    @property
    def suggested_subject(self) -> str | None:
        """
        The key's agent comment, when it is already a usable actor identifier.

        A convenience for authoring a trust root, and never more than that. The comment is a label
        the key's own holder typed into their agent: unauthenticated, unverified, and trivially
        set to anyone's address. It is offered so a maintainer does not have to retype what they
        already have, and it is never adopted on its own -- a subject is a claim this brain's
        governance makes, so a quorum has to make it deliberately.

        Returns:
            str | None: The comment if it is an actor identifier, otherwise ``None``. Most
            comments are (``alex@laptop`` is not, but ``alex@example.org`` is), and a comment that
            is not one is simply not offered rather than repaired.
        """
        from boltzmann.identity.principal import is_actor_id

        candidate = self.comment.strip().lower()
        return candidate if candidate and is_actor_id(candidate) else None

    def sign_blob(self, data: bytes) -> bytes:
        """
        Sign framed bytes through the agent.

        Args:
            data (bytes): The exact bytes to sign -- the SSHSIG signed-data blob.

        Returns:
            bytes: The RFC 4253 signature blob the agent returned, used verbatim.

        Raises:
            SignerUnavailableError: If the agent is gone, refuses, or does not hold the key.
        """
        from boltzmann.authenticity.agent import SshAgentClient

        with SshAgentClient(socket_path=self.socket_path, timeout=self.timeout) as agent:
            return agent.sign(self.public_key, data)
