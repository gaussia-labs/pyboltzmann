"""Canonical serialization: the function ``block_id`` is computed over.

The paper leaves the deterministic serialization used for ``block_id`` open
(Section 12). This SDK closes it as **JCS, RFC 8785**: JSON with keys sorted by
UTF-16 code point, no insignificant whitespace, and a fixed number format.

JCS was chosen over a binary encoding because a block is a small structured
record and the protocol is meant to be implemented by clients in several
languages: a canonical form that a human can read and ``grep`` is worth more here
than compactness, and RFC 8785 has implementations in JS, Go, Rust, Java, and
Python. The choice is recorded in every block envelope as ``"jcs/1"``, so a
future serialization can coexist with it rather than replace it.

**Two value domains are excluded from a payload.** JCS defines float
serialization through ECMAScript rules that are subtly hard to reproduce
identically across languages, and integers outside the IEEE-754 safe range lose
precision in any JSON parser backed by doubles. Since a divergence in either
would mean two conforming clients computing different ``block_id`` values for the
same knowledge, both are rejected at the schema level. Represent a decimal as a
string, or as an integer scaled by a documented factor.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import rfc8785

from boltzmann.exceptions import NonDeterministicValueError, SerializationError

SERIALIZATION_ID = "jcs/1"
"""Identifier of the canonical serialization this version of the protocol uses."""

MAX_SAFE_INTEGER = 2**53 - 1
"""Largest integer an IEEE-754 double represents exactly (ECMAScript ``Number.MAX_SAFE_INTEGER``)."""

MIN_SAFE_INTEGER = -MAX_SAFE_INTEGER
"""Smallest integer an IEEE-754 double represents exactly."""


@runtime_checkable
class Serializer(Protocol):
    """Turns a JSON-shaped value into the exact bytes that get hashed."""

    def __call__(self, value: Any) -> bytes: ...


def reject_non_deterministic(value: Any, path: str = "$") -> None:
    """
    Walk a JSON-shaped value and reject anything without a portable canonical form.

    Args:
        value (Any): The value to check, typically a block payload.
        path (str): JSON path of ``value``, used to locate the offender.

    Raises:
        NonDeterministicValueError: If a float, an out-of-range integer, or a
            non-string object key is found.
        SerializationError: If a type outside the JSON data model is found.
    """
    if value is None or isinstance(value, str | bool):
        return

    if isinstance(value, float):
        raise NonDeterministicValueError(
            f"float at {path}: floats have no portable canonical form and are not allowed in a block "
            f"payload; use a string, or an integer scaled by a documented factor"
        )

    if isinstance(value, int):
        if not MIN_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise NonDeterministicValueError(
                f"integer at {path} is outside the IEEE-754 safe range "
                f"[{MIN_SAFE_INTEGER}, {MAX_SAFE_INTEGER}] and would lose precision in a "
                f"double-backed JSON parser; represent it as a string"
            )
        return

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonDeterministicValueError(
                    f"non-string object key at {path}: {key!r} of type {type(key).__name__}"
                )
            reject_non_deterministic(item, f"{path}.{key}")
        return

    if isinstance(value, list | tuple):
        for position, item in enumerate(value):
            reject_non_deterministic(item, f"{path}[{position}]")
        return

    raise SerializationError(f"value at {path} of type {type(value).__name__} is outside the JSON data model")


def canonicalize_jcs(value: Any) -> bytes:
    """
    Serialize ``value`` per RFC 8785, after rejecting non-deterministic values.

    Args:
        value (Any): A JSON-shaped value.

    Returns:
        bytes: The canonical UTF-8 bytes to hash.

    Raises:
        NonDeterministicValueError: If the value holds a float or an unsafe integer.
        SerializationError: If RFC 8785 canonicalization fails.
    """
    reject_non_deterministic(value)
    try:
        return rfc8785.dumps(value)
    except rfc8785.CanonicalizationError as error:
        raise SerializationError(f"RFC 8785 canonicalization failed: {error}") from error


SERIALIZERS: dict[str, Serializer] = {
    SERIALIZATION_ID: canonicalize_jcs,
}
"""Registry of canonical serializations, keyed by the identifier stored in a block envelope."""


def get_serializer(serialization: str) -> Serializer:
    """
    Resolve a serialization identifier to its implementation.

    Args:
        serialization (str): An identifier such as ``"jcs/1"``.

    Returns:
        Serializer: The serializer that produces the bytes to hash.

    Raises:
        SerializationError: If the identifier is not registered.
    """
    try:
        return SERIALIZERS[serialization]
    except KeyError:
        known = ", ".join(sorted(SERIALIZERS))
        raise SerializationError(f"unknown serialization {serialization!r}; this client implements: {known}") from None


def canonicalize(value: Any, serialization: str = SERIALIZATION_ID) -> bytes:
    """
    Serialize ``value`` with the named canonical serialization.

    Args:
        value (Any): A JSON-shaped value.
        serialization (str): The serialization identifier. Defaults to ``"jcs/1"``.

    Returns:
        bytes: The canonical bytes to hash.
    """
    return get_serializer(serialization)(value)
