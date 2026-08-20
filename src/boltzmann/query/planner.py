"""The query planner interface: the part the protocol deliberately does not fix.

The protocol fixes the contract and the invariants, not the algorithm (paper Section 9.2). A
conforming planner must:

* return knowledge blocks with their provenance and a retrieval score, never prose;
* verify every returned block against the installed snapshot by hash and membership;
* treat no single index as authoritative.

Everything else is the implementation's: which indices to consult, how to fuse their rankings,
the ranking weights, the graph expansion depth, and any adaptive intent classification. In
practice retrieval is hybrid -- filters narrow a candidate set, lexical and vector search generate
candidates in parallel, their rankings are fused, the graph expands from the strongest hits, and
the hash map resolves and verifies each result -- but a planner that reaches the same verifiable
set another way is equally conforming.

A consequence worth restating: the protocol guarantees **verifiability, not identical ranking**.
Two conforming planners may return the same set of blocks in a different order, because vector
search is approximate and ranking is tunable. So there is no reference planner here to imitate.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.module.module import Module
from boltzmann.query.evidence import EvidenceBundle
from boltzmann.query.request import Query


@runtime_checkable
class QueryPlanner(Protocol):
    """Turns a declarative query into a verified Evidence Bundle. Implemented by the caller."""

    def plan(self, query: Query, modules: dict[MemoryType, Module]) -> EvidenceBundle:
        """
        Retrieve evidence for a query.

        Args:
            query (Query): The declarative request. It names no index; selecting and combining
                indices is this planner's job.
            modules (dict[MemoryType, Module]): The installed modules, keyed by memory type.

        Returns:
            EvidenceBundle: Verified matches with their provenance and scores.
        """
        ...
