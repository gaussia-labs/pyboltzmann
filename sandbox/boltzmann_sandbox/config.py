"""What has to be true before anything starts.

The one requirement that is not negotiable: **the OCI artifact has to be named**. A brain whose
publishing target is unknown is a brain you cannot test, and discovering that after the server is up
means discovering it inside a tool call, where the error surfaces as a failed request instead of a
failed startup.

So configuration is read and validated once, up front, and a missing value stops the process rather than
degrading it. :func:`load` raises; :func:`report` is the same set of checks rendered for a human, which
is what ``boltzmann-doctor`` prints.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from boltzmann.blocks.provenance import ActorKind, Collaborator
from dotenv import load_dotenv

TOKEN_URL: Final = "Docker Hub -> Account settings -> Personal access tokens"
"""Where a Docker Hub token comes from. Named once so every error message agrees."""

DEFAULT_TAG: Final = "latest"
DEFAULT_BRAIN_PATH: Final = "./brain"

HUB_INDEX_HOSTS: Final = frozenset({"docker.io", "index.docker.io"})
"""How people write Docker Hub, and what the registry API is not.

``docker.io`` is the *index* hostname. ``https://docker.io/v2/…`` serves Docker Hub's website, so a
registry client that takes the name literally gets HTTP 200 and a page of HTML where it expected a
manifest. The API lives at :data:`HUB_REGISTRY_HOST`.

``docker pull docker.io/user/repo`` works because the Docker CLI performs this substitution for you. A
library that does not is not wrong, but it is surprising, so the substitution happens here rather than
being left as a footnote in the README.
"""

HUB_REGISTRY_HOST: Final = "registry-1.docker.io"
"""Docker Hub's registry API endpoint."""


class ConfigError(Exception):
    """Configuration that cannot be repaired at runtime, only fixed in ``.env``."""


def _flag(name: str, default: bool = False) -> bool:
    """An environment variable read as a boolean, accepting the spellings people actually use."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _sandbox_actor() -> str:
    """A usable actor identifier when nobody set one.

    A login name is not an identifier: it names a person on one machine and nobody anywhere else,
    which is the whole reason the form exists. Namespacing it under ``sandbox`` keeps the fallback
    working and keeps it honest about what it is -- and anything that survives no character rule
    falls back to the namespace alone rather than producing a brain nobody can open.
    """
    from boltzmann.identity.principal import is_actor_id

    candidate = f"sandbox/{_text('USER', 'anonymous').lower()}"
    return candidate if is_actor_id(candidate) else "sandbox/anonymous"


def _resolve(value: str, source: Path) -> Path:
    """
    A configured path, made absolute.

    A relative path in a configuration file means relative to that file, not to whoever happened to start
    the process. Without this an MCP client that launches the server from the user's project would create
    the brain there, and ``./brain`` would name a different directory for every caller.

    Args:
        value (str): The configured path.
        source (Path): The ``.env`` it came from.

    Returns:
        Path: An absolute path.
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (source.parent / path).resolve()


def _registry_endpoint(reference: str) -> str:
    """
    A repository reference the transport can actually reach.

    Only Docker Hub needs this, and it needs it badly: ``docker.io`` is the index hostname, so a request to
    ``https://docker.io/v2/…`` lands on the website and comes back HTTP 200 with HTML. Nothing about that
    resembles a registry error, so the failure surfaces as a JSON parse error somewhere far from its cause.

    Args:
        reference (str): The repository as configured.

    Returns:
        str: The same repository, addressed at the registry API.
    """
    host, separator, rest = reference.partition("/")
    if separator and host in HUB_INDEX_HOSTS:
        return f"{HUB_REGISTRY_HOST}/{rest}"
    return reference


def default_env_file() -> Path:
    """
    Where to look for ``.env`` when the caller names no file.

    Beside the project rather than beside the *caller*, for two reasons. An MCP client starts the server
    with a working directory of its own choosing -- often the user's project, not this one -- so a relative
    ``.env`` would simply not be found, and the server would refuse to start while the file sat right
    there. And ``dotenv``'s own discovery walks the call stack, which raises rather than returns when there
    is no calling frame to walk, as in ``python -`` or an embedded interpreter.

    A ``.env`` in the current directory still wins if one is there, since a caller who put it there meant
    it.

    Returns:
        Path: The ``.env`` to read.
    """
    local = Path.cwd() / ".env"
    if local.is_file():
        return local
    return Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True, slots=True)
class Settings:
    """
    Everything this sandbox needs, validated.

    Attributes:
        registry (str): The repository the brain publishes to, ``<host>/<namespace>/<repo>``, with Docker
            Hub's index hostname already replaced by its registry endpoint. This is the string handed to
            the transport; :attr:`configured` is what was written in ``.env``.
        configured (str): The repository exactly as configured, for messages a human has to match against
            what they typed.
        tag (str): Tag that push and pull default to.
        brain_path (Path): The on-disk OCI layout. This directory *is* the brain.
        actor (str): Who registers knowledge. Provenance records it on every write, and it is
            hashed into every block that names it, so it must be an actor identifier: an
            address, or a namespaced name. Set ``BOLTZMANN_ACTOR`` to your own; the fallback
            derived from ``$USER`` is namespaced under ``sandbox/`` precisely because a bare
            login name means nothing on any other machine.
        agent (str): The runtime writing on the actor's behalf, when one is -- an MCP client, an
            agent harness. Empty when a person is driving directly. Recorded beside the actor, not
            instead of it: the actor is whose account the work runs under, and the agent is what
            did it.
        agent_model (str): The model that agent ran, when it is known. Empty otherwise, which is a
            smaller claim than naming a model that was guessed.
        username (str): Registry account, empty when anonymous.
        token (str): Registry token, empty when anonymous.
        anonymous (bool): Whether to talk to the registry without credentials.
        insecure (bool): Whether plain HTTP is allowed. Local registries only.
    """

    registry: str
    configured: str
    tag: str
    brain_path: Path
    actor: str
    agent: str
    agent_model: str
    username: str
    token: str
    anonymous: bool
    insecure: bool

    @property
    def assisting(self) -> list[Collaborator]:
        """Who takes part besides the actor, as provenance will record them.

        Empty when a person is working alone, which is what keeps those records at schema version 1
        with the bytes they would have had before assisting parties existed. The runtime and the
        model it ran stay one entry, because the same model under a different harness is a
        different collaborator.

        Returns:
            list[Collaborator]: The assisting parties, or empty.
        """
        if not self.agent:
            return []
        return [
            Collaborator(
                id=self.agent,
                kind=ActorKind.AGENT,
                model=self.agent_model or None,
            )
        ]

    @property
    def reference(self) -> str:
        """The artifact reference, tag included, as a registry would print it."""
        return f"{self.registry}:{self.tag}"

    @property
    def authenticated(self) -> bool:
        """Whether credentials are present to send."""
        return bool(self.username and self.token)

    @property
    def host(self) -> str:
        """The registry host, or Docker Hub's default when the reference names none."""
        head = self.registry.split("/", 1)[0]
        # A first segment is a host only if it looks like one; "library/redis" names no host.
        if "." in head or ":" in head or head == "localhost":
            return head
        return HUB_REGISTRY_HOST

    @property
    def is_docker_hub(self) -> bool:
        """Whether this points at Docker Hub, whose free-tier limits are worth warning about."""
        return self.host in HUB_INDEX_HOSTS | {HUB_REGISTRY_HOST}


def load(env_file: Path | str | None = None) -> Settings:
    """
    Read the environment and refuse to continue if it is incomplete.

    Values already in the environment win over the file, so a one-off override on the command line
    works without editing ``.env``.

    Args:
        env_file (Path | str | None): The ``.env`` to read. Defaults to one beside the working directory.

    Returns:
        Settings: Validated configuration.

    Raises:
        ConfigError: If a required value is missing or malformed.
    """
    source = Path(env_file) if env_file is not None else default_env_file()
    load_dotenv(source, override=False)

    registry = _text("BOLTZMANN_REGISTRY")
    if not registry:
        raise ConfigError(
            "BOLTZMANN_REGISTRY is not set, so there is no OCI artifact to work against. "
            "Set it to <host>/<namespace>/<repo> -- for Docker Hub, "
            "docker.io/<your-namespace>/boltzmann-sandbox. Start from .env.example."
        )
    if ":" in registry.rsplit("/", 1)[-1]:
        raise ConfigError(
            f"BOLTZMANN_REGISTRY carries a tag ({registry!r}). The repository and the tag are separate "
            f"here, because one brain publishes many versions: put the tag in BOLTZMANN_TAG."
        )
    if "/" not in registry:
        raise ConfigError(
            f"BOLTZMANN_REGISTRY is {registry!r}, which names no repository. A registry needs at least "
            f"<namespace>/<repo>."
        )

    anonymous = _flag("BOLTZMANN_ANONYMOUS")
    username, token = _text("DOCKER_USERNAME"), _text("DOCKER_TOKEN")
    if not anonymous and not (username and token):
        missing = " and ".join(
            name for name, value in (("DOCKER_USERNAME", username), ("DOCKER_TOKEN", token)) if not value
        )
        raise ConfigError(
            f"{missing} not set. Publishing to {registry} needs credentials: create a Personal Access "
            f"Token with the Read & Write scope at {TOKEN_URL} -- the account password will not do. "
            f"For a public read-only pull, or a local registry, set BOLTZMANN_ANONYMOUS=1 instead."
        )

    return Settings(
        registry=_registry_endpoint(registry),
        configured=registry,
        tag=_text("BOLTZMANN_TAG", DEFAULT_TAG),
        brain_path=_resolve(_text("BOLTZMANN_BRAIN_PATH", DEFAULT_BRAIN_PATH), source),
        actor=_text("BOLTZMANN_ACTOR", _sandbox_actor()),
        agent=_text("BOLTZMANN_AGENT"),
        agent_model=_text("BOLTZMANN_AGENT_MODEL"),
        username=username,
        token=token,
        anonymous=anonymous,
        insecure=_flag("BOLTZMANN_INSECURE"),
    )
