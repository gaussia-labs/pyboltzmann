"""Retrieval without an index: the protocol's part of a query, and nothing more.

A brain that was just opened has no index engine, because which engine backs an index is the
implementation's choice (paper Section 6.3). But ``search`` still has to work, so this module does the
part that belongs to the protocol rather than to a retrieval strategy:

1. **Filter** the installed compositions by what the query declared -- memory type, subject, recency,
   tags, cited evidence, whether superseded blocks are wanted.
2. **Resolve** each surviving block through its module, which checks membership.
3. **Verify** the bytes hash to the identity they are filed under.
4. **Report** provenance: which canonical evidence each match cites, and where in it.

**This is not a ranking strategy, and it does not pretend to be.** Matching is a case-insensitive term
scan over the text a block carries, the traversal is linear, and the score is the *fraction of query
terms present* -- coverage, not relevance. An implementation that wants relevance injects a
:class:`~boltzmann.query.planner.QueryPlanner` and replaces candidate generation entirely; verification
stays where it is either way.

Two things the scan does get for free, because the protocol stores them symbolically on the block
rather than only in a derived index:

* **Associative expansion.** ``relations`` live on semantic blocks, so following them to
  ``hints.expand_depth`` needs no graph engine.
* **Supersession.** The provenance ledger says which blocks a newer one replaced, so a superseded block
  can be held back unless the caller asks for it -- which is what Section 10.4 means by supersession
  changing accessibility rather than membership.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.blocks.canonical import CanonicalBlock
from boltzmann.blocks.episodic import EpisodicBlock
from boltzmann.blocks.procedural import ProceduralBlock
from boltzmann.blocks.semantic import SemanticBlock
from boltzmann.exceptions import DigestFormatError, DigestKindError
from boltzmann.identity.digest import BlockId
from boltzmann.module.ledger import Ledger
from boltzmann.query.evidence import EvidenceBundle, Match, SourceRef
from boltzmann.query.request import RetrievalMode

if TYPE_CHECKING:
    from boltzmann.blocks.base import Block
    from boltzmann.blocks.memory_type import MemoryType
    from boltzmann.module.module import Module
    from boltzmann.query.request import Query, QueryFilters

SCORE_PRECISION = 2
"""Decimal places in the coverage score. A string, because a wire format should not carry a float."""

_DETERMINERS = ("a", "an", "the", "this", "that", "these", "those")
_CONNECTIVES = ("and", "or", "but", "nor", "so", "then", "also", "if", "than")
_PREPOSITIONS = (
    "of", "to", "in", "on", "at", "by", "for", "from", "with", "without",
    "into", "onto", "over", "under", "about", "as",
)  # fmt: skip
_AUXILIARIES = (
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "done", "have", "has", "had", "having",
    "can", "could", "may", "might", "must", "shall", "should", "will", "would",
)  # fmt: skip
_PRONOUNS = (
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their",
)  # fmt: skip
_INTERROGATIVES = ("what", "when", "where", "which", "who", "whom", "whose", "why", "how")
_NEGATION_AND_PLACE = ("no", "not", "there", "here")

STOPWORDS = frozenset(
    _DETERMINERS + _CONNECTIVES + _PREPOSITIONS + _AUXILIARIES + _PRONOUNS + _INTERROGATIVES + _NEGATION_AND_PLACE
)
"""Words dropped from a query before matching.

Function words, chosen by grammatical role rather than by frequency: a list built from frequency would
eventually swallow a term some brain treats as knowledge, and a stopword too many is an answer nobody can
find. Nothing domain-specific belongs here.

They are excluded because including them makes the filter stop filtering -- ``an`` alone matched fourteen
of fifteen blocks in a brain that knew nothing about the query's subject. An implementation with its own
:class:`~boltzmann.query.planner.QueryPlanner` decides this for itself; this is what the built-in scan does.
"""


ProvenanceView = Ledger
"""Kept as a name for the ledger view, which both the query and the retention paths read."""


def searchable_text(block: Block) -> list[str]:
    """
    The text a block carries, as separate fields.

    Only what a block states symbolically. A canonical block has no prose -- it is a descriptor over
    bytes -- so it contributes its media type and nothing else, and will not match a natural-language
    query. That is correct: canonical memory records what was observed, not what it says.

    Args:
        block (Block): The block to read.

    Returns:
        list[str]: Its text-bearing fields.
    """
    if isinstance(block, SemanticBlock):
        return [block.label, block.statement, block.subject or "", *(block.aliases or [])]
    if isinstance(block, ProceduralBlock):
        return [
            block.label,
            block.goal,
            block.subject or "",
            *(step.action for step in block.steps),
            *(step.condition or "" for step in block.steps),
            *(block.preconditions or []),
            *(block.success_criteria or []),
        ]
    if isinstance(block, EpisodicBlock):
        return [
            block.summary,
            block.context or "",
            block.outcome or "",
            *(block.participants or []),
            *(block.tags or []),
        ]
    if isinstance(block, CanonicalBlock):
        return [block.media_type]
    return []


def _subject_of(block: Block) -> str | None:
    return getattr(block, "subject", None)


def _tags_of(block: Block) -> list[str]:
    return list(getattr(block, "tags", None) or [])


def _evidence_of(block: Block) -> list[BlockId]:
    return list(getattr(block, "evidence", None) or [])


def _passes_filters(block: Block, filters: QueryFilters) -> bool:
    """Whether a block survives the narrowing conditions the query declared."""
    if filters.subject is not None and _subject_of(block) != filters.subject:
        return False

    if filters.tags and not set(filters.tags) <= set(_tags_of(block)):
        return False

    if filters.evidence and not set(filters.evidence) & set(_evidence_of(block)):
        return False

    if filters.since is not None or filters.until is not None:
        occurred = getattr(block, "occurred_at", None)
        if occurred is None:
            # A block with no time cannot satisfy a recency window.
            return False
        if filters.since is not None and occurred < filters.since:
            return False
        if filters.until is not None and occurred > filters.until:
            return False

    return True


def content_terms(text: str) -> list[str]:
    """
    The words of a query that carry retrieval signal.

    Function words are dropped, because counting them makes the filter stop filtering. A query mentioning
    ``an`` matched fourteen of fifteen blocks in a brain that knew nothing about the subject, and every one
    of them then had a score: the term was present, so coverage was above zero, so the block was a match.
    Removing them also fixes the ranking, because the denominator stops rewarding a block for sharing
    grammar rather than meaning.

    A query that is nothing but function words keeps them. Answering ``"what is it"`` with nothing found
    would be worse than answering it badly, and a caller who typed only function words has told us nothing
    to narrow by.

    Args:
        text (str): The query as written.

    Returns:
        list[str]: The terms to match on, case-folded.
    """
    words = [word for word in text.casefold().split() if word]
    content = [word for word in words if word not in STOPWORDS]
    return content or words


def _coverage(terms: list[str], block: Block) -> float:
    """The fraction of query terms present in a block's text. Coverage, not relevance."""
    if not terms:
        return 1.0
    haystack = " ".join(searchable_text(block)).casefold()
    return sum(1 for term in terms if term in haystack) / len(terms)


def _expand(
    matched: dict[BlockId, float],
    modules: dict[MemoryType, Module],
    depth: int,
) -> dict[BlockId, float]:
    """Follow declared relations outward, which needs no graph engine because they live on the blocks."""
    if depth <= 0:
        return matched

    reachable = dict(matched)
    frontier = set(matched)
    for _ in range(depth):
        discovered: set[BlockId] = set()
        for block_id in frontier:
            for module in modules.values():
                if block_id not in module or not module.store.is_resolvable(block_id):
                    continue
                block = module.get(block_id)
                if not isinstance(block, SemanticBlock) or not block.relations:
                    continue
                for relation in block.relations:
                    if relation.target not in reachable:
                        discovered.add(relation.target)
        if not discovered:
            break
        for block_id in discovered:
            # Reached by association, not by matching, so it carries no term coverage of its own.
            reachable[block_id] = 0.0
        frontier = discovered
    return reachable


def scan(query: Query, modules: dict[MemoryType, Module]) -> EvidenceBundle:
    """
    Filter, resolve, and verify, without consulting any index.

    Args:
        query (Query): The declarative request.
        modules (dict[MemoryType, Module]): The installed modules.

    Returns:
        EvidenceBundle: Verified matches, ordered by term coverage. Every match is checked by hash and
        by membership in the installed snapshot; a block that fails either is left out rather than
        returned unverified.
    """
    view = Ledger.of(modules)
    searched = _modules_to_search(query, modules)

    if query.hints.mode is RetrievalMode.EXACT:
        candidates = _exact(query, searched)
    else:
        terms = content_terms(query.text)
        candidates = {
            block_id: coverage
            for memory_type, module in searched.items()
            for block_id in module.block_ids
            if module.store.is_resolvable(block_id)
            and _passes_filters(module.get(block_id), query.filters)
            and (coverage := _coverage(terms, module.get(block_id))) > 0
        }
        candidates = _expand(candidates, searched, query.hints.expand_depth)

    if not query.filters.include_superseded:
        # Supersession and demotion both change accessibility rather than membership, so a block held
        # back here is still in the composition and still proves into the root.
        candidates = {block_id: score for block_id, score in candidates.items() if view.is_accessible(block_id)}

    ordered = sorted(candidates.items(), key=lambda pair: (-pair[1], pair[0].hex))
    matches = [
        match
        for block_id, score in ordered[: query.limit]
        if (match := _to_match(block_id, score, searched, view)) is not None
    ]

    return EvidenceBundle(
        matches=matches,
        verified_against={memory_type: module.root for memory_type, module in searched.items()},
        truncated=len(ordered) > query.limit,
    )


def _modules_to_search(query: Query, modules: dict[MemoryType, Module]) -> dict[MemoryType, Module]:
    """Which modules the query asked for, restricted to what is installed."""
    if query.filters.memory_types is None:
        return dict(modules)
    return {kind: modules[kind] for kind in query.filters.memory_types if kind in modules}


def _exact(query: Query, modules: dict[MemoryType, Module]) -> dict[BlockId, float]:
    """Resolve the query text as an identity. Not a ranked guess, so the score carries no gradation."""
    try:
        block_id = BlockId.parse(query.text)
    except (DigestFormatError, DigestKindError):
        return {}
    return {block_id: 1.0} if any(block_id in module for module in modules.values()) else {}


def _to_match(
    block_id: BlockId,
    score: float,
    modules: dict[MemoryType, Module],
    view: Ledger,
) -> Match | None:
    """Build one verified match, or ``None`` if the block does not belong to any searched module."""
    for memory_type, module in modules.items():
        if block_id not in module:
            continue

        resolvable = module.store.is_resolvable(block_id)
        content: dict = {}
        sources: list[SourceRef] = []
        if resolvable:
            # Reading through the module checks membership; the store checks the hash.
            block = module.get(block_id)
            content = block.payload()
            locator = view.locators.get(block_id)
            sources = [SourceRef(block_id=cited, locator=locator) for cited in _evidence_of(block)]

        return Match(
            block_id=block_id,
            memory_type=memory_type,
            content=content,
            score=f"{score:.{SCORE_PRECISION}f}",
            sources=sources,
            verified=True,
            resolvable=resolvable,
            superseded_by=view.superseded_by.get(block_id),
        )
    return None
