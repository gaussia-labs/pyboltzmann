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

import json
from abc import ABC
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict, model_validator

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import BlockIntegrityError, BlockSchemaError
from boltzmann.identity.digest import BlockId
from boltzmann.identity.serialization import SERIALIZATION_ID, canonicalize, reject_non_deterministic

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
            envelope = json.loads(data)
        except json.JSONDecodeError as error:
            raise BlockSchemaError(f"block envelope is not valid JSON: {error}") from error

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
                f"no schema registered for {memory_type} version {schema_version!r}; this client knows {known}"
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
