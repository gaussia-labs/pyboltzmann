"""The block envelope and the computation of ``block_id`` (paper Section 6.1).

A block is a logical unit of knowledge with a deterministic serialization:

.. math::

    block\\_id = SHA\\text{-}256(canonical\\_serialization(block))

What gets hashed is the **envelope**, not the payload alone, so that the memory
type and the schema version are bound into the identity:

.. code-block:: json

    {
      "boltzmann": 1,
      "memory_type": "semantic",
      "payload": {},
      "schema_version": 1,
      "serialization": "jcs/1"
    }

Everything derived or mutable stays outside: physical location, index state, and
the time a block happened to be registered are not part of what the block *is*.

Two conventions keep identity unambiguous:

* An absent optional field is dropped from the payload rather than serialized as
  ``null``, so ``{"a": 1}`` and ``{"a": 1, "b": null}`` are the same block.
  Optional collections therefore default to ``None``, never to ``[]``.
* Blocks are frozen. Correcting a block means creating a new one; the previous
  one may be kept for history, audit, or rollback.
"""

from __future__ import annotations

import logging
from abc import ABC
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic import ValidationError as PydanticValidationError

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import BlockIntegrityError, BlockSchemaError, SerializationError
from boltzmann.identity.digest import BlockId, Digest
from boltzmann.identity.serialization import SERIALIZATION_ID, canonicalize, parse_json_strict, reject_non_deterministic

ENVELOPE_KEYS = frozenset({"boltzmann", "memory_type", "payload", "schema_version", "serialization"})
"""The exact set of keys a block envelope carries."""

_REGISTRY: dict[tuple[MemoryType, int], type[Block]] = {}


class Block(BaseModel, ABC):
    """
    Base class for the five typed knowledge blocks.

    A subclass declares its memory type and schema version as class variables and
    its fields as the payload. Identity, serialization, and decoding come for free.

    Attributes:
        MEMORY_TYPE (MemoryType): Which memory module this block belongs to.
        SCHEMA_VERSION (int): Version of this block's payload schema.
        SERIALIZATION (str): Canonical serialization used to compute ``block_id``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    MEMORY_TYPE: ClassVar[MemoryType]
    SCHEMA_VERSION: ClassVar[int] = 1
    SERIALIZATION: ClassVar[str] = SERIALIZATION_ID

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Register concrete subclasses so stored bytes can be decoded back."""
        super().__init_subclass__(**kwargs)
        memory_type = getattr(cls, "MEMORY_TYPE", None)
        if memory_type is None or ABC in cls.__bases__:
            return
        key = (memory_type, cls.SCHEMA_VERSION)
        registered = _REGISTRY.get(key)
        if registered is not None and registered is not cls:
            raise BlockSchemaError(
                f"{cls.__name__} claims ({memory_type}, schema_version={cls.SCHEMA_VERSION}), "
                f"already held by {registered.__name__}"
            )
        _REGISTRY[key] = cls
        _warn_if_unregistered(memory_type, cls)

    @model_validator(mode="after")
    def _reject_non_deterministic_payload(self) -> Self:
        """Fail at construction, not at hashing, when a value has no canonical form."""
        reject_non_deterministic(self.payload())
        return self

    # --- Identity -------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        """
        The block's payload as a JSON-shaped mapping.

        Returns:
            dict[str, Any]: The payload, with absent optional fields dropped.
        """
        return self.model_dump(mode="json", exclude_none=True)

    def envelope(self) -> dict[str, Any]:
        """
        The full envelope that ``block_id`` is computed over.

        Returns:
            dict[str, Any]: The envelope mapping.
        """
        return {
            "boltzmann": PROTOCOL_VERSION,
            "memory_type": self.MEMORY_TYPE.value,
            "payload": self.payload(),
            "schema_version": self.SCHEMA_VERSION,
            "serialization": self.SERIALIZATION,
        }

    def canonical_bytes(self) -> bytes:
        """
        The exact bytes that are hashed and stored.

        Returns:
            bytes: The canonically serialized envelope.
        """
        return canonicalize(self.envelope(), self.SERIALIZATION)

    @property
    def block_id(self) -> BlockId:
        """The block's content-addressed identity."""
        return BlockId.of(self.canonical_bytes())

    # --- Content --------------------------------------------------------------

    @property
    def content_digests(self) -> tuple[Digest, ...]:
        """
        The bytes this block names but does not carry.

        A payload is JSON, so a block whose datum is large or binary names it by
        digest and leaves the bytes in the store; see
        :class:`~boltzmann.blocks.content.ContentRef`. Everything that has to
        account for those bytes -- packing a layer, marking reachability before a
        prune, destroying them on redaction -- asks the block here rather than
        testing its type, so a schema that starts naming content is handled by all
        of them at once.

        Returns:
            tuple[Digest, ...]: The content addresses, empty for a self-contained
            block. Order is not significant; callers deduplicate.
        """
        return ()

    # --- Decoding -------------------------------------------------------------

    @staticmethod
    def decode(data: bytes) -> Block:
        """
        Decode stored bytes back into a typed block.

        Beyond parsing, this checks that ``data`` is already in canonical form: if
        re-serializing the decoded block does not reproduce ``data`` byte for byte,
        the stored bytes would hash to a different ``block_id`` than they claim, so
        they are rejected rather than silently normalized.

        Args:
            data (bytes): Canonically serialized envelope bytes.

        Returns:
            Block: The decoded block.

        Raises:
            BlockSchemaError: If the envelope is malformed or its type is unknown.
            BlockIntegrityError: If ``data`` is not in canonical form.
        """
        try:
            envelope = parse_json_strict(data)
        except SerializationError as error:
            raise BlockSchemaError(f"block envelope {error}") from error

        if not isinstance(envelope, dict):
            raise BlockSchemaError(f"block envelope must be an object, got {type(envelope).__name__}")

        missing = ENVELOPE_KEYS - envelope.keys()
        unexpected = envelope.keys() - ENVELOPE_KEYS
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing {sorted(missing)}")
            if unexpected:
                details.append(f"unexpected {sorted(unexpected)}")
            raise BlockSchemaError(f"malformed block envelope: {', '.join(details)}")

        if envelope["boltzmann"] != PROTOCOL_VERSION:
            raise BlockSchemaError(
                f"block declares protocol version {envelope['boltzmann']!r}, this client implements {PROTOCOL_VERSION}"
            )

        try:
            memory_type = MemoryType(envelope["memory_type"])
        except ValueError as error:
            raise BlockSchemaError(f"unknown memory type {envelope['memory_type']!r}") from error

        schema_version = envelope["schema_version"]
        block_class = _REGISTRY.get((memory_type, schema_version))
        if block_class is None:
            known = sorted(version for kind, version in _REGISTRY if kind is memory_type)
            raise BlockSchemaError(
                f"no schema registered for {memory_type} version {schema_version!r}; this client knows {known}. "
                f"A block declaring a version this client does not implement was written by a newer SDK, so "
                f"upgrade boltzmann to read it -- the schema cannot be inferred from the bytes"
            )

        if envelope["serialization"] != block_class.SERIALIZATION:
            raise BlockSchemaError(
                f"block declares serialization {envelope['serialization']!r}, "
                f"{block_class.__name__} uses {block_class.SERIALIZATION!r}"
            )

        block = block_class.model_validate(envelope["payload"])
        if block.canonical_bytes() != data:
            raise BlockIntegrityError(
                f"stored bytes for a {memory_type} block are not in canonical "
                f"{block_class.SERIALIZATION} form, so they do not hash to the block_id they claim"
            )
        return block

    @staticmethod
    def registry() -> dict[tuple[MemoryType, int], type[Block]]:
        """
        The registered block schemas.

        Returns:
            dict[tuple[MemoryType, int], type[Block]]: Map of memory type and
            schema version to the class that implements it.
        """
        return dict(_REGISTRY)

    @staticmethod
    def schemas(memory_type: MemoryType) -> tuple[type[Block], ...]:
        """
        Every registered schema for a memory type, oldest version first.

        Args:
            memory_type (MemoryType): Which kind of block.

        Returns:
            tuple[type[Block], ...]: The classes, ordered by ``SCHEMA_VERSION``.

        Raises:
            BlockSchemaError: If no schema is registered for that memory type.
        """
        versions = sorted(version for kind, version in _REGISTRY if kind is memory_type)
        if not versions:
            raise BlockSchemaError(f"no schema registered for {memory_type.value} blocks")
        return tuple(_REGISTRY[(memory_type, version)] for version in versions)

    @staticmethod
    def build(memory_type: MemoryType, payload: dict[str, Any]) -> Block:
        """
        Build a block under the **oldest** registered schema its payload satisfies.

        Choosing the newest instead would mean that registering a schema anywhere in the
        process silently re-versions every block written afterwards, including the ones
        that use nothing the new schema added. Since ``schema_version`` is part of the
        envelope, and therefore of ``block_id``, that is not a cosmetic difference: it
        makes an artifact unreadable to every consumer that has not upgraded, to record
        knowledge those consumers could have read perfectly well.

        Oldest-that-fits inverts that. A payload naming no content still validates under
        v1 and is written as v1, so a brain only stops being readable by an older client
        at the point where it genuinely uses something that client has no schema for.

        The candidate does not get to choose. A version is not a preference -- it is a
        statement about which fields the payload uses, which the payload itself already
        answers.

        Args:
            memory_type (MemoryType): Which kind of block to build.
            payload (dict[str, Any]): The block's payload.

        Returns:
            Block: The typed block, under the oldest schema that accepts the payload.

        Raises:
            BlockSchemaError: If no schema is registered for that memory type.
            pydantic.ValidationError: If no registered schema accepts the payload. The
                error comes from the closest matching shape; ties prefer the newest schema.
        """
        schemas = Block.schemas(memory_type)
        failures: list[PydanticValidationError] = []
        for block_class in schemas:
            try:
                return block_class.model_validate(dict(payload))
            except PydanticValidationError as error:
                failures.append(error)
        # Schema versions may be sibling shapes rather than an ever-widening inheritance chain.
        # Report the closest shape; on a tie prefer the newest vocabulary.
        raise min(enumerate(failures), key=lambda item: (len(item[1].errors()), -item[0]))[1].with_traceback(None)


def _warn_if_unregistered(memory_type: MemoryType, cls: type[Block]) -> None:
    """Say something when a schema is defined that the protocol's registry does not carry.

    ``schema_version`` sits inside the envelope and therefore inside ``block_id``, so "registered"
    cannot mean "whatever this process happens to define". It means present in the companion
    document versioned with the protocol (paper Section 6.6). A schema only this deployment knows
    produces blocks only this deployment can name -- two parties holding identical knowledge compute
    different identifiers for it, which is the silent divergence canonical serialization exists to
    prevent, re-entering through the version field.

    A warning rather than a refusal, and deliberately. Defining a schema is how one comes to be
    proposed for registration in the first place, and an exception here would make the SDK unusable
    for the work that precedes registration. What must not happen is for it to pass unremarked.
    """
    try:
        from boltzmann.conformance import golden

        published = golden.registry()["schemas"]
    except Exception:
        return

    versions = {entry["schema_version"] for entry in published.get(memory_type.value, ())}
    if cls.SCHEMA_VERSION in versions:
        return

    logging.getLogger(__name__).warning(
        "%s defines %s schema_version %d, which the schema registry does not carry. Blocks written "
        "under it are named in a way no other implementation reproduces, so they will not be "
        "recognized as the same knowledge elsewhere. Register the schema in the protocol's companion "
        "document (%s) before writing blocks under it",
        cls.__name__,
        memory_type.value,
        cls.SCHEMA_VERSION,
        golden.CORPUS_REPOSITORY,
    )
