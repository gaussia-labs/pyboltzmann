"""What an actor identifier is, and why it has a form at all (paper Section 5).

A provenance record names who performed an operation, and a provenance record is a block: the
identifier enters the payload, the payload enters the envelope, and the envelope is what
``block_id`` is computed over. So an identifier two parties spell differently is not a cosmetic
disagreement -- it is two identifiers for one person, and therefore two names for one fact, which
is precisely the silent divergence canonical serialization exists to prevent, re-entering through
a field nobody canonicalized.

Two forms, and no third:

* an **address**, ``local@domain`` -- a person, by something they already hold;
* a **namespaced name**, ``namespace/name`` -- a person known only by a handle, an organization,
  a runtime, a model, or a pipeline. The namespace carries whoever made or vouches for the name,
  which is why nothing anywhere repeats it as a separate field.

No scheme prefix is stored. ``mailto:`` is ceremony around a value people already write correctly,
and a URL invites exactly the questions a canonical form must leave closed: a trailing slash, a
default port, a percent-encoded octet. An implementation exporting to a provenance format that
requires URIs derives one at that boundary.

**Rejected, never normalized.** Lowercasing an identifier would mint a ``block_id`` the caller did
not ask for and cannot predict, which makes it one they cannot search for either. The house rule
everywhere else canonical bytes matter applies here unchanged.
"""

from __future__ import annotations

from enum import StrEnum

from boltzmann.exceptions import ActorIdError

MAX_ACTOR_ID = 320
"""Longest identifier accepted, matching the practical bound on an address: 64 + 1 + 255.

Bounded for the same reason every other decoded length in the protocol is: the value arrives
inside an artifact, so its size is chosen by whoever wrote the artifact.
"""

MAX_LOCAL_PART = 64
MAX_DOMAIN = 255
MAX_LABEL = 63
MAX_SEGMENT = 128

_LOWER = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
"""The alphanumeric core. Lowercase only, and ASCII only.

An internationalized address is carried in its ASCII form. Admitting Unicode here would put IDN
mapping and normalization between a caller and their own identifier, and two implementations that
mapped differently would be back to two names for one person.
"""

_LOCAL_EXTRA = frozenset("._%+-")
_LABEL_EXTRA = frozenset("-")
_SEGMENT_EXTRA = frozenset("._-")


class ActorIdForm(StrEnum):
    """Which of the two forms an identifier takes."""

    ADDRESS = "address"
    """``local@domain``."""

    NAMESPACED = "namespaced"
    """``namespace/name``."""


def _runs_over(value: str, allowed: frozenset[str]) -> bool:
    return bool(value) and all(char in allowed for char in value)


def _bounded_by_alphanumerics(value: str) -> bool:
    return value[0] in _LOWER and value[-1] in _LOWER


def _valid_label(label: str) -> bool:
    if not 1 <= len(label) <= MAX_LABEL:
        return False
    return _runs_over(label, _LOWER | _LABEL_EXTRA) and _bounded_by_alphanumerics(label)


def _valid_domain(domain: str) -> bool:
    if not 1 <= len(domain) <= MAX_DOMAIN:
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    return all(_valid_label(label) for label in labels)


def _valid_segment(segment: str) -> bool:
    if not 1 <= len(segment) <= MAX_SEGMENT:
        return False
    return _runs_over(segment, _LOWER | _SEGMENT_EXTRA) and _bounded_by_alphanumerics(segment)


def _refusal(value: str) -> str | None:
    """Why an identifier is not acceptable, or ``None`` when it is.

    A reason per refusal, deliberately. A caller who cannot tell *what* is wrong with an
    identifier works around the check instead of fixing the value.
    """
    if not value:
        return "it is empty"
    if len(value) > MAX_ACTOR_ID:
        return f"it is {len(value)} characters, over the {MAX_ACTOR_ID} an identifier may take"
    for char in value:
        if char.isspace():
            return "it carries whitespace, which has no canonical treatment here"
        if not 0x20 < ord(char) < 0x7F:
            return f"it carries {char!r}, and an identifier is printable ASCII"
    if any(char.isupper() for char in value):
        return "it is not lowercase, and an identifier is refused rather than lowered"

    if "@" in value and "/" in value:
        return "it carries both '@' and '/', so which form it takes is ambiguous"

    if "@" in value:
        local, _, domain = value.partition("@")
        if not local:
            return "its local part is empty"
        if len(local) > MAX_LOCAL_PART:
            return f"its local part is over {MAX_LOCAL_PART} characters"
        if not _runs_over(local, _LOWER | _LOCAL_EXTRA):
            return "its local part carries a character the address form does not admit"
        if local[0] == "." or local[-1] == ".":
            return "its local part begins or ends with a dot"
        if ".." in local:
            return "its local part carries consecutive dots, which have no single spelling"
        if not domain:
            return "its domain is empty"
        if "." not in domain:
            return "its domain has one label, so it names nothing off this machine"
        if not _valid_domain(domain):
            return "its domain is not a sequence of labels of [a-z0-9-] bounded by alphanumerics"
        return None

    if "/" not in value:
        return "it is neither an address nor a namespaced name, so it names nothing off this machine"
    if value.count("/") > 1:
        return "it carries more than one '/', and a namespaced name takes exactly one"

    namespace, _, name = value.partition("/")
    if not namespace:
        return "its namespace is empty"
    if not name:
        return "its name is empty"
    if not _valid_segment(namespace):
        return "its namespace is not [a-z0-9._-] bounded by alphanumerics"
    if not _valid_segment(name):
        return "its name is not [a-z0-9._-] bounded by alphanumerics"
    return None


def actor_id_form(value: str) -> ActorIdForm | None:
    """
    Which form an identifier takes, or ``None`` when it takes neither.

    The predicate behind :func:`parse_actor_id`, exposed because a reader auditing a brain full of
    legacy identifiers wants to classify them without catching an exception per record.

    Args:
        value (str): The identifier to classify.

    Returns:
        ActorIdForm | None: The form, or ``None`` if the value is not an actor identifier.
    """
    if _refusal(value) is not None:
        return None
    return ActorIdForm.ADDRESS if "@" in value else ActorIdForm.NAMESPACED


def is_actor_id(value: str) -> bool:
    """
    Whether a value is an actor identifier.

    Args:
        value (str): The identifier to check.

    Returns:
        bool: Whether it takes one of the two forms.
    """
    return _refusal(value) is None


def parse_actor_id(value: str, *, field: str = "actor id") -> str:
    """
    Return an identifier unchanged, or refuse it and say why.

    Returns the value rather than a wrapper type, and unchanged rather than normalized: the whole
    point is that what a caller wrote is what gets hashed.

    Args:
        value (str): The identifier.
        field (str): What is being validated, for the message -- so an error about an assisting
            party's model does not read as one about the actor.

    Returns:
        str: ``value``, byte for byte.

    Raises:
        ActorIdError: If the value takes neither form, with the reason and an example of each.
    """
    reason = _refusal(value)
    if reason is None:
        return value
    raise ActorIdError(
        f"{field} {value!r} is not usable: {reason}. An actor identifier is an address such as "
        f"'alex@example.org' or a namespaced name such as 'anthropic/claude-code', lowercase, and "
        f"it is refused rather than rewritten because it is hashed into every block that names it"
    )
