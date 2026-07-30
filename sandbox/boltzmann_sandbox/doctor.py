"""Everything that has to hold before the server starts, checked while you can still read the output.

The server validates its configuration in its lifespan and dies if it is wrong, which is correct but
inconvenient: over stdio a startup failure reaches the client as a broken transport rather than as an
explanation. So the same checks run here first, one line each, and the exit code says whether starting is
worth attempting.

The checks go from cheap to expensive and stop being informative once one fails hard, so a missing
variable is not followed by a network error about it.
"""

from __future__ import annotations

import importlib.metadata
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from boltzmann_sandbox.config import ConfigError, Settings, load

if TYPE_CHECKING:
    from collections.abc import Iterator

OK: Final = "ok"
WARN: Final = "warn"
FAIL: Final = "fail"

_MARK: Final = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}

SDK_PACKAGE: Final = "boltzmann"


@dataclass(frozen=True, slots=True)
class Check:
    """
    One diagnostic.

    Attributes:
        status (str): :data:`OK`, :data:`WARN` or :data:`FAIL`.
        label (str): What was checked.
        detail (str): What was found, and for a failure what to do about it.
    """

    status: str
    label: str
    detail: str

    def render(self) -> str:
        """The check as one line of terminal output."""
        return f"[{_MARK[self.status]}] {self.label:24s} {self.detail}"


def check_sdk() -> Iterator[Check]:
    """
    Whether the SDK is installed, and whether it is the current one.

    ``[tool.uv.sources]`` installs ``boltzmann`` as a built wheel rather than an editable, so editing
    ``../src/boltzmann`` does not reach this environment until it is reinstalled. Comparing timestamps
    catches the confusing case: code that was changed, and behaviour that did not follow.
    """
    try:
        version = importlib.metadata.version(SDK_PACKAGE)
    except importlib.metadata.PackageNotFoundError:
        yield Check(FAIL, "boltzmann installed", "not installed. Run: uv sync")
        return

    import boltzmann

    installed = Path(boltzmann.__file__).parent
    yield Check(OK, "boltzmann installed", f"{version} from {installed.parent.name}/")

    sources = Path(__file__).resolve().parent.parent.parent / "src" / SDK_PACKAGE
    if not sources.is_dir():
        # Installed from an index rather than from the sibling checkout. Nothing to compare against.
        return

    newest_source = max((path.stat().st_mtime for path in sources.rglob("*.py")), default=0.0)
    newest_installed = max((path.stat().st_mtime for path in installed.rglob("*.py")), default=0.0)
    if newest_source > newest_installed:
        yield Check(
            WARN,
            "boltzmann is current",
            "the sources are newer than the installed copy. Run: uv sync --reinstall-package boltzmann",
        )
    else:
        yield Check(OK, "boltzmann is current", "matches ../src/boltzmann")


def check_settings() -> tuple[Settings | None, list[Check]]:
    """
    Read the configuration, reporting what is missing rather than raising.

    Returns:
        tuple[Settings | None, list[Check]]: The settings when valid, and the checks to print.
    """
    try:
        settings = load()
    except ConfigError as error:
        return None, [Check(FAIL, "configuration", str(error))]

    checks = [
        Check(
            OK,
            "artifact",
            settings.reference
            if settings.configured == settings.registry
            else f"{settings.configured}:{settings.tag} -> {settings.reference}",
        ),
        Check(
            OK if settings.authenticated else WARN,
            "credentials",
            f"{settings.username} (token present)"
            if settings.authenticated
            else "anonymous. Reading a public repository works; pushing will not",
        ),
        Check(OK, "actor", settings.actor),
    ]

    if settings.is_docker_hub and settings.insecure:
        checks.append(
            Check(FAIL, "transport", "BOLTZMANN_INSECURE=1 against Docker Hub. Plain HTTP is for local registries")
        )
    else:
        checks.append(Check(OK, "transport", "http (insecure)" if settings.insecure else "https"))

    return settings, checks


def check_brain(settings: Settings) -> Iterator[Check]:
    """Whether the local brain opens, what version it is on, and what a push would carry."""
    try:
        from boltzmann_sandbox.brain import open_brain

        brain = open_brain(settings)
    except Exception as error:
        yield Check(FAIL, "local brain", f"{settings.brain_path} did not open: {error}")
        return

    snapshot = brain.snapshot()
    if not snapshot.modules:
        yield Check(OK, "local brain", f"{settings.brain_path} (empty; ready for a first commit)")
        return

    blocks = sum(reference.block_count for reference in snapshot.modules.values())
    yield Check(
        OK,
        "local brain",
        f"{snapshot.digest.short} -- {len(snapshot.modules)} modules, {blocks} blocks, "
        f"{len(brain.ancestry())} versions",
    )

    # An index that cannot be rebuilt is absent unless this process built it or the layout already held it,
    # and a push would then publish the module without it. Honest, and easy to miss, so it is said here.
    ready = brain.travelling_indices
    expected = {memory_type for memory_type in brain.indices if memory_type in snapshot.modules}
    missing = sorted(kind.value for kind in expected - ready)
    if missing:
        yield Check(
            WARN,
            "travelling index",
            f"absent for {', '.join(missing)} -- a push from this process would publish those modules "
            f"without their vector index. Pack from the process that committed, or pull first",
        )
    elif ready:
        yield Check(OK, "travelling index", f"present for {', '.join(sorted(kind.value for kind in ready))}")


async def check_registry(settings: Settings) -> Iterator[Check]:
    """
    Whether the registry answers, authenticates, and already holds this tag.

    Resolving the tag is the one check that cannot be faked: it is the same call ``pull`` makes, against
    the same reference, with the same credentials.
    """
    from boltzmann.exceptions import DistributionError

    from boltzmann_sandbox.brain import registry_client

    checks: list[Check] = []
    try:
        client = registry_client(settings)
    except Exception as error:
        return iter(
            [
                Check(
                    FAIL,
                    "registry login",
                    f"{settings.host} refused {settings.username}: {error}. A Personal Access Token with "
                    f"the Read & Write scope is required; an account password will not do",
                )
            ]
        )

    if settings.authenticated:
        checks.append(Check(OK, "registry login", f"{settings.host} accepted {settings.username}"))

    try:
        manifest = await client.resolve(settings.registry, settings.tag)
    except DistributionError as error:
        message = str(error)
        # A tag that does not exist yet is the normal state before the first push, not a problem.
        expected_absence = any(token in message.lower() for token in ("not found", "404", "manifest unknown"))
        checks.append(
            Check(
                OK if expected_absence else WARN,
                "remote tag",
                f"{settings.reference} does not exist yet; the first push creates it"
                if expected_absence
                else f"could not resolve {settings.reference}: {message}",
            )
        )
    else:
        modules = ", ".join(
            sorted(layer.annotations.get("ai.gaussia.boltzmann.memory-type", "?") for layer in manifest.layers)
        )
        checks.append(Check(OK, "remote tag", f"{settings.reference} resolves -- layers: {modules or 'none'}"))

    return iter(checks)


async def diagnose() -> list[Check]:
    """
    Run every check, in order, stopping at the first that makes the rest meaningless.

    Returns:
        list[Check]: What to print.
    """
    checks = list(check_sdk())
    if any(check.status == FAIL for check in checks):
        return checks

    settings, configured = check_settings()
    checks.extend(configured)
    if settings is None or any(check.status == FAIL for check in configured):
        return checks

    checks.extend(check_brain(settings))
    checks.extend(await check_registry(settings))
    return checks


def main() -> int:
    """
    Print the diagnosis and exit non-zero if the server could not start.

    Returns:
        int: ``0`` when everything that matters holds, ``1`` otherwise.
    """
    import asyncio

    print("boltzmann sandbox -- preflight\n")
    checks = asyncio.run(diagnose())
    for check in checks:
        print(check.render())

    failures = [check for check in checks if check.status == FAIL]
    warnings = [check for check in checks if check.status == WARN]
    print()
    if failures:
        print(f"{len(failures)} blocking problem(s). Fix them in .env and run this again.")
        return 1

    summary = "ready" if not warnings else f"ready, with {len(warnings)} warning(s)"
    print(f"{summary}. Start the server with: uv run boltzmann-mcp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
