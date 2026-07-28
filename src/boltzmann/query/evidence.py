"""The Evidence Bundle: data, never prose.

The brain's result is not a written answer. It is a data contract carrying matching
blocks, content, memory type, relations, sources, retrieval score, block ids, and
verification status (paper Section 9.3):

.. code-block:: json

    {
      "matches": [{
        "block_id": "sha256:CONCEPT1",
        "memory_type": "semantic",
        "content": "<serialized knowledge block content>",
        "source": {"block_id": "sha256:PDF123", "page": 147},
        "verified": true
      }]
    }

**There is no field for an answer.** Not omitted for brevity -- absent by design. The
consumer receives identities, content, memory type, sources, and verification status,
and remains free to explain, summarize, compare, or reuse the evidence. Those
transformations happen outside the brain, which is what keeps the architecture
independent of any provider.

The bundle also carries the roots it was verified against, so ``verified: true`` is a
checkable claim about a named snapshot rather than an assertion the caller has to trust.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import MembershipError
from boltzmann.identity.digest import BlockId, MerkleRoot


class SourceRef(BaseModel):
    """
    Where a match came from.

    Attributes:
        block_id (BlockId): The canonical block cited as evidence.
        locator (str | None): Position within the source. The paper's example is a page
            number; a locator generalizes that so a line range, a section, or a media
            timestamp fits the same field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: BlockId
    locator: str | None = None


class Match(BaseModel):
    """
    One retrieved block, with everything needed to audit it.

    Attributes:
        block_id (BlockId): Identity of the returned block.
        memory_type (MemoryType): Which module it came from.
        content (dict[str, Any]): The block's payload.
        score (str): Retrieval score, as a decimal string. Scores are strings for the
            same reason payloads forbid floats: a number whose textual form varies
            across languages does not belong in a wire format meant to be compared.
        sources (list[SourceRef]): The canonical evidence this block cites.
        verified (bool): Whether the block's hash and its membership in the installed
            snapshot were both checked.
        resolvable (bool): Whether the block's bytes are still present. ``False`` means
            redacted, which a consumer must be able to tell apart from corrupted
            (paper Section 10.6).
        superseded_by (BlockId | None): A newer block that takes precedence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    block_id: BlockId
    memory_type: MemoryType
    content: dict[str, Any]
    score: str
    sources: list[SourceRef] = Field(default_factory=list)
    verified: bool = False
    resolvable: bool = True
    superseded_by: BlockId | None = None


class EvidenceBundle(BaseModel):
    """
    What a query returns.

    Attributes:
        matches (list[Match]): The retrieved blocks, in the implementation's ranking.
            Two conforming implementations may return the same verifiable set in a
            different order: the protocol guarantees verifiability, not identical
            ranking (paper Section 9.2).
        verified_against (dict[MemoryType, MerkleRoot]): The module roots membership was
            checked against, so the claim can be re-checked independently.
        truncated (bool): Whether matches were dropped to respect the result limit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    matches: list[Match] = Field(default_factory=list)
    verified_against: dict[MemoryType, MerkleRoot] = Field(default_factory=dict)
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.matches)

    @property
    def all_verified(self) -> bool:
        """Whether every match was verified by hash and by membership."""
        return all(match.verified for match in self.matches)

    def require_verified(self) -> None:
        """
        Fail unless every match was verified.

        A conforming implementation must verify every returned block against the
        installed snapshot (paper Section 9.2), so an unverified match reaching a caller
        is a bug in the implementation, not a soft result to be weighed.

        Raises:
            MembershipError: If any match is unverified.
        """
        unverified = [match.block_id.short for match in self.matches if not match.verified]
        if unverified:
            raise MembershipError(
                f"the bundle contains unverified matches, which a conforming implementation "
                f"must never return: {', '.join(unverified)}"
            )
