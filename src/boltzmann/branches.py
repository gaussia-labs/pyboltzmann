"""Branch names and the tags they publish to (paper Section 7.5).

A branch is a named pointer to a snapshot. Locally it is an entry in the ``refs`` pointer; on a registry it
is a tag. This module holds the part of that which is pure: what a valid name is, and the reversible
mapping between a name and its tag. The mapping is fixed by the protocol rather than by deployments, so a
branch one client publishes is a branch to every other client that lists the repository.
"""

from __future__ import annotations

import re

from boltzmann.exceptions import InvalidBranchNameError

DEFAULT_BRANCH = "main"
"""The branch a brain without a ref table has, and the one that publishes to the default tag."""

DEFAULT_TAG = "latest"
"""The tag the default branch publishes to unless a deployment treats another tag as current."""

BRANCH_TAG_PREFIX = "br."
"""What every non-default branch's tag begins with. Fixed by the protocol, not configurable."""

MAX_TAG_LENGTH = 128
"""The longest tag OCI allows."""

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_NAME = re.compile(rf"{_SEGMENT}(?:/{_SEGMENT})*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")


def validate_branch_name(name: str) -> str:
    """
    Check a branch name against the grammar.

    A name is one or more segments separated by ``/``, each matching ``[A-Za-z0-9][A-Za-z0-9_-]*``.
    Segments contain no ``.``, which is what keeps the tag mapping reversible.

    Args:
        name (str): The proposed name.

    Returns:
        str: The name, unchanged.

    Raises:
        InvalidBranchNameError: If the name violates the grammar or its tag would exceed what OCI allows.
    """
    if not _NAME.fullmatch(name):
        raise InvalidBranchNameError(
            f"{name!r} is not a branch name: use segments of letters, digits, '_' and '-', separated by '/', "
            f"each starting with a letter or digit"
        )
    if name != DEFAULT_BRANCH and len(BRANCH_TAG_PREFIX) + len(name) > MAX_TAG_LENGTH:
        raise InvalidBranchNameError(
            f"branch {name!r} would publish to a tag longer than the {MAX_TAG_LENGTH} characters OCI allows"
        )
    return name


def tag_for(name: str, default_tag: str = DEFAULT_TAG) -> str:
    """
    The tag a branch publishes to.

    Args:
        name (str): The branch.
        default_tag (str): What the default branch publishes to.

    Returns:
        str: ``default_tag`` for the default branch, otherwise ``br.`` followed by the name with each
        ``/`` replaced by ``.``.

    Raises:
        InvalidBranchNameError: If the name is not a valid branch name.
    """
    validate_branch_name(name)
    if name == DEFAULT_BRANCH:
        return default_tag
    return BRANCH_TAG_PREFIX + name.replace("/", ".")


def branch_for_tag(tag: str, default_tag: str = DEFAULT_TAG) -> str | None:
    """
    The branch a tag names, if it names one.

    Args:
        tag (str): A published tag.
        default_tag (str): What the default branch publishes to.

    Returns:
        str | None: The default branch for ``default_tag``, the decoded name for a ``br.`` tag that decodes
        to a valid name, otherwise ``None`` -- a release tag such as ``v1`` names no branch.
    """
    if tag == default_tag:
        return DEFAULT_BRANCH
    if not tag.startswith(BRANCH_TAG_PREFIX) or not _TAG.fullmatch(tag):
        return None
    name = tag[len(BRANCH_TAG_PREFIX) :].replace(".", "/")
    try:
        validate_branch_name(name)
    except InvalidBranchNameError:
        return None
    if name == DEFAULT_BRANCH:
        return None  # ``br.main`` is not the default branch, and no other branch may be called main.
    return name
