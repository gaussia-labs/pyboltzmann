"""A selective artifact's config: a view bound to a real snapshot.

A projection deliberately is not a snapshot. It has no parents, timestamp, labels, or trust root
of its own and is never signed. Its authority and position come entirely from ``source``; the
module references here must be a verbatim subset of that source snapshot.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.exceptions import DistributionError, SerializationError
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.serialization import canonicalize, parse_json_strict
from boltzmann.module.snapshot import ModuleRef


class Projection(BaseModel):
    """A source snapshot digest and the module references retained from it verbatim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    source: OciDigest
    modules: dict[MemoryType, ModuleRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _module_keys_match_references(self) -> Self:
        for memory_type, reference in self.modules.items():
            if reference.memory_type is not memory_type:
                raise ValueError(
                    f"projection module key {memory_type.value!r} carries a {reference.memory_type.value!r} reference"
                )
        return self

    @property
    def installed(self) -> list[MemoryType]:
        """Retained modules in canonical module order."""
        return [kind for kind in MemoryType if kind in self.modules]

    def canonical_bytes(self) -> bytes:
        """The canonical config bytes stored and digested by OCI."""
        return canonicalize(self.model_dump(mode="json"))

    @classmethod
    def from_document(cls, data: bytes) -> Projection:
        """Decode a canonical projection document without ambiguous JSON semantics."""
        try:
            document = parse_json_strict(data)
            projection = cls.model_validate(document)
        except (SerializationError, ValueError) as error:
            raise DistributionError(f"projection document cannot be read: {error}") from error
        if projection.canonical_bytes() != data:
            raise DistributionError("projection document is not in canonical jcs/1 form")
        return projection

    @property
    def digest(self) -> OciDigest:
        """The projection document's physical identity."""
        return OciDigest.of(self.canonical_bytes())
