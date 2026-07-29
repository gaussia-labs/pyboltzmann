"""A query planner, which the protocol deliberately does not fix.

The protocol fixes the contract and the invariants, not the algorithm (paper Section 9.2): return blocks
with their provenance and a score, never prose; verify every block returned against the installed
snapshot; treat no single index as authoritative. Which indices to consult and how to combine them is
the implementation's.

So this planner splits the work the way the paper does:

* **Filtering, resolution and verification stay with the protocol.** They are delegated to
  :func:`boltzmann.query.scan.scan`, which already narrows by the query's filters, resolves each block
  through its module (checking membership), verifies the bytes against the identity they are filed
  under, and reports which canonical evidence each match cites. A third-party planner does not have to
  reimplement any of that, and should not: verification that each implementation writes for itself is
  verification nobody can trust.
* **Candidate ranking is the planner's.** Three rankings are fused with Reciprocal Rank Fusion: the
  scan's term coverage, the inverted index, and the vector index. RRF combines rankings rather than
  scores, so an inverted index's idf weights and a vector index's cosine similarities -- which share no
  scale -- can be merged without inventing a conversion between them.

**What this does not do is make retrieval faster.** The scan is linear, and calling it means traversing
the composition. What fusion buys here is ranking quality and, more to the point, a real exercise of the
index code paths. A planner built for scale would generate candidates *from* its indices and verify only
those, using the module to resolve; the shape of the verification step would be the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from boltzmann.query.evidence import EvidenceBundle
from boltzmann.query.request import RetrievalMode
from boltzmann.query.scan import scan

if TYPE_CHECKING:
    from boltzmann.blocks.memory_type import MemoryType
    from boltzmann.identity.digest import BlockId
    from boltzmann.indices.base import Index
    from boltzmann.module.module import Module
    from boltzmann.query.evidence import Match
    from boltzmann.query.request import Query

RRF_K: Final = 4
"""The rank offset in Reciprocal Rank Fusion. It damps the influence of the top of any single ranking,
which is what stops one confident index from dominating the fusion.

The classic value is 60, tuned against TREC runs of a thousand documents each. At that magnitude the
difference between ``1/(60+1)`` and ``1/(60+2)`` is under two percent, so against a module of a few dozen
blocks every result comes back scoring within a hair of every other -- the ordering is right and the
score is useless. A small offset restores the spread at the sizes a brain actually produces. It is a free
parameter of the method, not a deviation from it."""

CANDIDATE_MULTIPLIER: Final = 5
"""How much wider than the requested limit to gather before fusing. Fusing a list already truncated to
the limit would rank only what one strategy happened to surface, which defeats the point."""

MIN_CANDIDATE_POOL: Final = 50
"""A floor on the pool, so a query for three results still has something to fuse."""

SCORE_PRECISION: Final = 4
"""Decimals in the reported score. A string on the wire, like the SDK's own, because a wire format
should not carry a float."""


class HybridPlanner:
    """
    Fuses the scan, an inverted index and a vector index into one ranking.

    Attributes:
        indices (dict[MemoryType, list[Index]]): The same indices the brain maintains. The planner reads
            them; the brain owns rebuilding them, because the protocol's rule is that only the commit
            path writes to an index.
    """

    def __init__(self, indices: dict[MemoryType, list[Index]] | None = None) -> None:
        """
        Build a planner over a brain's indices.

        Args:
            indices (dict[MemoryType, list[Index]] | None): Indices per memory type. With none, this
                degrades to the scan's own ranking, which is still conforming -- just not hybrid.
        """
        self.indices = dict(indices or {})

    def plan(self, query: Query, modules: dict[MemoryType, Module]) -> EvidenceBundle:
        """
        Retrieve evidence for a query.

        Args:
            query (Query): The declarative request. It names no index; choosing them is this method's job.
            modules (dict[MemoryType, Module]): The installed modules.

        Returns:
            EvidenceBundle: Verified matches, re-ranked. Every match came through the scan, so every one
            was resolved through its module and checked by hash.
        """
        bundle = scan(self._widened(query), modules)

        # An identity lookup is not a ranked guess. Fusing it with approximate rankings would reorder an
        # exact answer, so the modes that mean "resolve this" are left alone.
        if query.hints.mode is RetrievalMode.EXACT or not bundle.matches:
            return self._truncated(bundle, query)

        fused = self._fuse(query, bundle.matches)
        return EvidenceBundle(
            matches=fused[: query.limit],
            verified_against=bundle.verified_against,
            truncated=bundle.truncated or len(fused) > query.limit,
        )

    # --- Fusion ---------------------------------------------------------------

    def _fuse(self, query: Query, matches: list[Match]) -> list[Match]:
        """Merge the scan's ranking with each index's, and restate the scores."""
        eligible = {match.block_id for match in matches}
        rankings: list[list[BlockId]] = [[match.block_id for match in matches]]

        for memory_type in {match.memory_type for match in matches}:
            for index in self.indices.get(memory_type, []):
                candidates = index.search(query.text, limit=self._pool(query))
                # An index may know about blocks the filters excluded, or that a newer version dropped.
                # The scan's result is the authority on what may be returned; fusion only reorders it.
                ranked = [block_id for block_id, _ in candidates if block_id in eligible]
                if ranked:
                    rankings.append(ranked)

        scores: dict[BlockId, float] = {}
        for ranking in rankings:
            for position, block_id in enumerate(ranking, start=1):
                scores[block_id] = scores.get(block_id, 0.0) + 1.0 / (RRF_K + position)

        # The theoretical maximum: first place in every ranking. Dividing by it puts the score in 0..1,
        # where 1.0 means every strategy ranked this block first.
        #
        # What it is not is a confidence: RRF scores a *position*, so second of two scores the same as
        # second of fifty. Read it as "how strongly the strategies agreed on this block relative to the
        # others in this result", the way the SDK's own scan reports coverage rather than relevance.
        ceiling = len(rankings) / (RRF_K + 1)
        by_id = {match.block_id: match for match in matches}
        ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0].hex))

        return [
            by_id[block_id].model_copy(update={"score": f"{score / ceiling:.{SCORE_PRECISION}f}"})
            for block_id, score in ordered
        ]

    # --- Candidate pool -------------------------------------------------------

    def _pool(self, query: Query) -> int:
        """How many candidates to gather from each source before fusing."""
        return max(query.limit * CANDIDATE_MULTIPLIER, MIN_CANDIDATE_POOL)

    def _widened(self, query: Query) -> Query:
        """The same query, asking for enough results that fusion has something to work with."""
        return query.model_copy(update={"hints": query.hints.model_copy(update={"limit": self._pool(query)})})

    def _truncated(self, bundle: EvidenceBundle, query: Query) -> EvidenceBundle:
        """Undo the widening for a bundle that will not be fused."""
        return EvidenceBundle(
            matches=bundle.matches[: query.limit],
            verified_against=bundle.verified_against,
            truncated=bundle.truncated or len(bundle.matches) > query.limit,
        )
