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

from dotenv import load_dotenv

TOKEN_URL: Final = "Docker Hub -> Account settings -> Personal access tokens"
"""Where a Docker Hub token comes from. Named once so every error message agrees."""

DEFAULT_TAG: Final = "latest"
DEFAULT_BRAIN_PATH: Final = "./brain"


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


@dataclass(frozen=True, slots=True)
class Settings:
    """
    Everything this sandbox needs, validated.

    Attributes:
        registry (str): The repository the brain publishes to, ``<host>/<namespace>/<repo>``.
        tag (str): Tag that push and pull default to.
        brain_path (Path): The on-disk OCI layout. This directory *is* the brain.
        actor (str): Who registers knowledge. Provenance records it on every write.
        username (str): Registry account, empty when anonymous.
        token (str): Registry token, empty when anonymous.
        anonymous (bool): Whether to talk to the registry without credentials.
        insecure (bool): Whether plain HTTP is allowed. Local registries only.
    """

    registry: str
    tag: str
    brain_path: Path
    actor: str
    username: str
    token: str
    anonymous: bool
    insecure: bool

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
        return "docker.io"

    @property
    def is_docker_hub(self) -> bool:
        """Whether this points at Docker Hub, whose free-tier limits are worth warning about."""
        return self.host in {"docker.io", "index.docker.io", "registry-1.docker.io"}


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
    load_dotenv(env_file, override=False)

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
        registry=registry,
        tag=_text("BOLTZMANN_TAG", DEFAULT_TAG),
        brain_path=Path(_text("BOLTZMANN_BRAIN_PATH", DEFAULT_BRAIN_PATH)).expanduser(),
        actor=_text("BOLTZMANN_ACTOR", _text("USER", "sandbox")),
        username=username,
        token=token,
        anonymous=anonymous,
        insecure=_flag("BOLTZMANN_INSECURE"),
    )
