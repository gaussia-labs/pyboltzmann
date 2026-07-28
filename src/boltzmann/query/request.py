"""A declarative query. It never names an index.

Principle 7: queries are declarative and index-agnostic. The caller expresses intent --
a query, optional filters over memory type, subject, or recency, and optional hints such
as a retrieval mode or a result limit -- and never names a physical index. Choosing which
indices to use, and how to combine them, is the responsibility of a conforming
implementation, in the same way that the SQL standard defines a language and its
semantics while each database ships its own optimizer (paper Section 9.2).

That is why there is no ``index`` field anywhere below, and why :class:`RetrievalMode`
names *strategies* rather than engines: ``LEXICAL`` says what kind of match the caller
wants, not that an inverted index must serve it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.identity.digest import BlockId
from boltzmann.identity.time import Timestamp

DEFAULT_LIMIT = 10
"""Result limit applied when the caller states none."""


class RetrievalMode(StrEnum):
    """How the caller wants matching to work. A strategy, not an engine."""

    AUTO = "auto"
    """Let the implementation infer intent from the query's shape. The default."""

    EXACT = "exact"
    """Resolve identities only."""

    LEXICAL = "lexical"
    """Match terms as written."""

    SEMANTIC = "semantic"
    """Match meaning, accepting approximation."""

    ASSOCIATIVE = "associative"
    """Expand outward from the strongest hits along declared relations."""


class QueryFilters(BaseModel):
    """
    Narrowing conditions over the installed snapshot.

    Attributes:
        memory_types (list[MemoryType] | None): Restrict to certain kinds of memory.
            This is what keeps "what happened in the class of May 14" from competing
            with "the definition of a Fourier series" in one similarity ranking
            (paper Section 4.2, R2).
        subject (str | None): Restrict to a domain.
        since (Timestamp | None): Earliest time considered, for episodic recency.
        until (Timestamp | None): Latest time considered.
        tags (list[str] | None): Restrict to blocks carrying all of these labels.
        include_superseded (bool): Whether blocks a newer one supersedes may be
            returned. Superseded blocks stay in the composition and remain verifiable;
            what changes is accessibility (paper Section 10.4).
        evidence (list[BlockId] | None): Restrict to blocks citing this evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_types: list[MemoryType] | None = None
    subject: str | None = None
    since: Timestamp | None = None
    until: Timestamp | None = None
    tags: list[str] | None = None
    include_superseded: bool = False
    evidence: list[BlockId] | None = None


class QueryHints(BaseModel):
    """
    Advice a planner may follow or ignore.

    A hint is not a directive. An implementation that ignores every hint and still
    returns verified blocks with their provenance is conforming.

    Attributes:
        mode (RetrievalMode): The matching strategy the caller prefers.
        limit (int): Maximum number of matches wanted.
        expand_depth (int): How far to follow relations, for associative retrieval.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: RetrievalMode = RetrievalMode.AUTO
    limit: int = Field(default=DEFAULT_LIMIT, ge=1)
    expand_depth: int = Field(default=0, ge=0)


class Query(BaseModel):
    """
    What a caller asks a brain for.

    Attributes:
        text (str): The query, in natural language or as an identifier. May be empty, which asks for
            everything the filters admit -- "the episodes of last May" is a complete request with no
            terms in it, and refusing it would make recency and subject filters unusable on their own.
        filters (QueryFilters): Narrowing conditions.
        hints (QueryHints): Advice for the planner.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = ""
    filters: QueryFilters = Field(default_factory=QueryFilters)
    hints: QueryHints = Field(default_factory=QueryHints)

    @property
    def is_filter_only(self) -> bool:
        """Whether the request carries no terms, so only the filters narrow it."""
        return not self.text.strip()

    @property
    def limit(self) -> int:
        """The requested result limit."""
        return self.hints.limit
