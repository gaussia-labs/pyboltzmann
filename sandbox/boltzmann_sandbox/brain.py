"""Opening a brain with the implementer's half filled in.

:meth:`boltzmann.Brain.open` takes a planner, indices, validators and a retention policy, and defaults
every one of them to nothing -- correctly, because they are the implementation's choice. This is one set
of those choices, in one place, so the MCP server and the demo open the same brain.

Two details worth stating, because both are protocol rules rather than preferences:

* The planner reads the same index objects the brain rebuilds. It does not keep its own copies, because
  an index the planner maintained itself could drift from the composition, and a stale index is how a
  query starts returning blocks the current version no longer contains.
* Dropping canonical evidence is off by default in the SDK, since excluding evidence forfeits
  re-derivation from it. It is switched on here because a sandbox that cannot demonstrate the privileged
  cascade of Section 10.3 cannot demonstrate the interesting half of retention.
"""

from __future__ import annotations

import sys
from pathlib import Path

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import Actor, ActorKind
from boltzmann.brain import Brain
from boltzmann.distribution.oras_client import OrasRegistryClient
from boltzmann.indices.base import Index
from boltzmann.retention.policy import RetentionPolicy

from boltzmann_sandbox.config import Settings
from boltzmann_sandbox.indices import InvertedIndex, VectorIndex
from boltzmann_sandbox.planner import HybridPlanner

INDEXED: tuple[MemoryType, ...] = (MemoryType.SEMANTIC, MemoryType.EPISODIC, MemoryType.PROCEDURAL)
"""The memory types worth indexing here.

Canonical memory is left out on purpose: a canonical block is a descriptor over bytes, not prose, so it
carries nothing to match on and would only add empty postings. Provenance is left out because it is
queried by relation rather than by text, and the SDK reads it through the ledger.
"""


def build_indices() -> dict[MemoryType, list[Index]]:
    """
    A fresh set of indices, one pair per indexed memory type.

    Each memory type gets its own instances rather than sharing: a module is versioned independently, so
    an index shared across two of them would be rebuilt by whichever committed last.

    Returns:
        dict[MemoryType, list[Index]]: An inverted and a vector index per indexed memory type.
    """
    return {memory_type: [InvertedIndex(), VectorIndex()] for memory_type in INDEXED}


def open_brain(settings: Settings, path: Path | None = None) -> Brain:
    """
    Open the brain this sandbox works against.

    Args:
        settings (Settings): Validated configuration.
        path (Path | None): Where the OCI layout lives. Defaults to the configured brain path; the demo
            passes an explicit one to install into a second, empty brain.

    Returns:
        Brain: A brain with a hybrid planner, indices, canonical drops permitted, and whatever
        agent is configured recorded beside the actor on every write.
    """
    indices = build_indices()
    return Brain.open(
        path if path is not None else settings.brain_path,
        actor=Actor(id=settings.actor, kind=ActorKind.HUMAN),
        assisted_by=settings.assisting,
        planner=HybridPlanner(indices),
        indices=indices,
        policy=RetentionPolicy(canonical_drop_allowed=True),
    )


def registry_client(settings: Settings) -> OrasRegistryClient:
    """
    A transport for the configured registry, authenticated if credentials were given.

    Args:
        settings (Settings): Validated configuration.

    Returns:
        OrasRegistryClient: Ready to resolve, pull and push.

    Raises:
        Exception: Whatever the registry raises when it refuses the credentials. Failing here is better
            than failing inside a push, where half the blobs are already uploaded.
    """
    _quiet_oras()
    client = OrasRegistryClient(insecure=settings.insecure)
    _ignore_docker_config(client)
    if settings.authenticated:
        client.login(settings.username, settings.token)
    return client


def _ignore_docker_config(client: OrasRegistryClient) -> None:
    """Keep ORAS away from the Docker credential store, which can hang the process.

    Before every request ORAS resolves credentials for the registry, and if ``~/.docker/config.json``
    names a ``credsStore`` it shells out to the matching helper -- ``docker-credential-desktop`` on a Mac
    running Docker Desktop. That ``subprocess.run`` carries **no timeout**, so a helper that blocks blocks
    the whole run, with no output and no error: the symptom is a push that never returns.

    Pre-seeding an empty credential set is what stops the lookup, because ORAS loads the config only once
    and only when it has none. It costs nothing, because credentials here are explicit by design --
    ``DOCKER_USERNAME`` and ``DOCKER_TOKEN``, or anonymous -- and an explicit ``login`` sets them by a
    different path that this does not touch.

    The consequence worth knowing: a prior ``docker login`` does **not** authenticate this sandbox. State
    the token in the environment.

    Args:
        client (OrasRegistryClient): The client to isolate.
    """
    auth = getattr(client.registry, "auth", None)
    if auth is None or not hasattr(auth, "_auth_config"):
        # A newer ORAS that reorganized its auth backend. Not worth failing over, but worth saying out
        # loud: without the workaround the symptom is a run that hangs with no output, which is a bad thing
        # to have to rediscover.
        print(
            "warning: could not isolate ORAS from the Docker credential store. If a push or pull hangs "
            "with no output, that is why -- see _ignore_docker_config in boltzmann_sandbox/brain.py",
            file=sys.stderr,
        )
        return

    auth._auth_config = {"auths": {}, "credsStore": None, "credHelpers": {}}


def _quiet_oras() -> None:
    """Stop ORAS narrating to stderr.

    It reports a failed Docker credential helper whenever one is configured and unused -- expected when
    talking to a local registry anonymously -- and prints ``manifest unknown`` for a tag that does not
    exist yet, which the caller already handles. Neither is an error here, and both read like one.

    Nothing is lost by silencing it: the SDK wraps every ORAS failure in a ``DistributionError`` carrying
    the same message, so a real problem still arrives, attributed and in context.

    ORAS writes to stderr rather than stdout, so this is legibility, not correctness: an MCP server over
    stdio would survive the noise either way.
    """
    import logging

    # Its own `quiet` flag only gates info-level messages; warnings and errors go to a standard logger.
    logging.getLogger("oras").setLevel(logging.CRITICAL)
    try:
        import oras.logger

        oras.logger.logger.quiet = True
    except (ImportError, AttributeError):
        # A newer ORAS that reorganized its logging. Noisy output is not worth failing over.
        pass
