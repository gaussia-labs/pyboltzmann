"""Canonical memory: what material was observed (paper Section 5).

The canonical module preserves observed evidence, not truth claims. A canonical
block asserts that a source was incorporated and that it *contains* certain
bytes; it does not declare those bytes correct. Because every semantic and
procedural interpretation cites canonical evidence through provenance, this
module is the root of re-derivation.

**A canonical block is a pure statement about observed bytes.** Its identity
depends on nothing else, which is what makes re-registering an identical original
a genuine no-op: two actors ingesting the same PDF compute the same ``block_id``
and the second registration adds no block. Everything historical or
actor-dependent -- who incorporated it, when, from where, under what license or
retention policy, and which earlier edition it supersedes -- is recorded as a
provenance edge instead. See :mod:`boltzmann.blocks.provenance`.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from boltzmann.blocks.base import Block
from boltzmann.blocks.content import ContentRef
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import Digest, OciDigest


class NormalizedView(ContentRef):
    """
    A deterministic transform of an original blob.

    A normalized view is plain text, Markdown, or a structured extract produced by
    a named deterministic pipeline whose identity is recorded in provenance.
    Normalized views live in canonical rather than semantic memory because they are
    still evidence for ingestion, not consolidated knowledge.

    Adds no field to :class:`~boltzmann.blocks.content.ContentRef`, and must not: the
    three it inherits are exactly what a normalized view is, and any change to the
    shape would change every canonical ``block_id`` that carries one. It stays a
    distinct name because *what* a view is -- evidence produced by a named pipeline --
    is not the same as the general fact that a block names bytes.
    """


class CanonicalBlock(Block):
    """
    Evidence that a source was incorporated and preserved.

    Note which level of identity ``blob`` carries. The observed bytes of a source
    are a transportable file, not a unit of knowledge, so they are addressed by an
    :class:`~boltzmann.identity.digest.OciDigest`; the ``block_id`` of this block is
    the knowledge-level statement *about* those bytes. Read together with
    ``media_type`` and ``size``, this block is an OCI descriptor over the evidence
    plus an optional normalized view of it -- which is why publishing a brain is a
    copy rather than a conversion.

    Attributes:
        blob (OciDigest): Content address of the original bytes as observed.
        media_type (str): IANA media type of the original.
        size (int): Length of the original in bytes.
        normalized_view (NormalizedView | None): Optional deterministic transform
            of the original, addressed by its own hash.
    """

    MEMORY_TYPE: ClassVar[MemoryType] = MemoryType.CANONICAL

    blob: OciDigest
    media_type: str = Field(min_length=1)
    size: int = Field(ge=0)
    normalized_view: NormalizedView | None = None

    @property
    def content_digests(self) -> tuple[Digest, ...]:
        """The observed bytes, and the normalized view when one was produced."""
        if self.normalized_view is None:
            return (self.blob,)
        return (self.blob, self.normalized_view.blob)
