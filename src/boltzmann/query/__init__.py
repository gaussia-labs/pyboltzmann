"""Query types and the protocol's part of retrieval.

The planner is an interface; ``search`` is declared on
:class:`~boltzmann.protocol.operations.BrainReader`. :func:`~boltzmann.query.scan.scan` is what a brain
with no index engine falls back to: filter, resolve, verify -- not a ranking strategy.
"""

from boltzmann.query.evidence import EvidenceBundle, Match, SourceRef
from boltzmann.query.planner import QueryPlanner
from boltzmann.query.request import DEFAULT_LIMIT, Query, QueryFilters, QueryHints, RetrievalMode
from boltzmann.query.scan import ProvenanceView, scan, searchable_text

__all__ = [
    "DEFAULT_LIMIT",
    "EvidenceBundle",
    "Match",
    "ProvenanceView",
    "Query",
    "QueryFilters",
    "QueryHints",
    "QueryPlanner",
    "RetrievalMode",
    "SourceRef",
    "scan",
    "searchable_text",
]
