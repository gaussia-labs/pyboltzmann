"""Two index engines, because the SDK ships none.

Which engine backs an index is the implementation's choice (paper Section 6.3), so ``boltzmann`` defines
the interface and stops there. That leaves the code paths that *use* an index -- rebuilding on commit,
packing the travelling one into a layer, loading it after a pull -- exercised only by test doubles. These
two are real enough to run those paths for real.

They are examples, not recommendations. Both are deliberately small:

* :class:`InvertedIndex` is term postings with idf-weighted overlap. Structural, therefore rebuildable,
  therefore it never travels: any client can regenerate it from the blocks.
* :class:`VectorIndex` is feature hashing into a fixed number of dimensions. It reports
  ``rebuildable = False`` and satisfies :class:`~boltzmann.indices.base.TravellingIndex`, because a
  model-agnostic client carries no embedding model and so cannot rebuild it. It is the one index that
  ships inside its module's layer, and the one that has to say what produced it.

The similarity :class:`VectorIndex` computes is lexical in disguise -- hashing bag-of-words puts no two
synonyms near each other. That is a fair trade for a sandbox: it is deterministic, needs no download, and
exercises the travelling-index contract exactly as a real embedding model would. Swap in real embeddings
by changing ``_embed`` and :attr:`VectorIndex.MODEL_TAG` together; the tag is what stops a consumer from
mixing two representation spaces.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from typing import Any, ClassVar, Final

from boltzmann.blocks.base import Block
from boltzmann.identity.digest import BlockId
from boltzmann.indices.base import AbstractIndex, IndexKind
from boltzmann.query.scan import searchable_text

_WORD = re.compile(r"[a-z0-9]+")
"""Tokens are runs of letters and digits, after case folding. Crude, and identical on every platform."""

MIN_TOKEN_LENGTH: Final = 2
"""Single characters carry no retrieval signal and inflate every posting list."""

MIN_STEM_LENGTH: Final = 4
"""How much of a word a suffix rule has to leave behind.

Below this, stripping does more harm than good: ``ties`` would become ``t`` and collide with everything.
Four keeps ``removing`` -> ``remov`` while leaving ``uses`` and ``this`` alone.
"""

_SUFFIXES: Final = ("ional", "ings", "edly", "ing", "ies", "ely", "est", "ed", "es", "ly", "s", "e")
"""Suffixes stripped, longest first so that ``ings`` wins over ``s``.

English inflection and the handful of derivations that show up in the same sentence as their root. Ordered
rather than sorted at import, because the order *is* the rule.

The trailing ``e`` is what makes the family close. English drops it before ``-ing`` and ``-es``, so
``remove`` keeps it while ``removing`` and ``removes`` lose it; stripping it from all three lands them on
``remov``. Without that rule the stem of a verb never matches the stem of the same verb inflected, which
was the miss this exists to fix.
"""


class IndexFormatError(Exception):
    """A published index that cannot be loaded into this one."""


def stem(token: str) -> str:
    """
    A token reduced to its rough root, so that inflections of one word are one token.

    Without this, ``remove`` and ``removing`` land in different posting lists and different hash buckets,
    and a question asking what happens when you *remove* something gets no credit from the block that says
    what happens when you *remove* it -- observed, and the wrong block won.

    Suffix stripping only, shortest useful suffix last, and never below :data:`MIN_STEM_LENGTH` so that
    short words survive intact. It is not Porter: ``was`` does not become ``be`` and ``better`` does not
    become ``good``. It closes the gap between a word and its own inflections, which is where the misses
    came from, and a real implementation would use a real stemmer -- or embeddings, which need none.

    Args:
        token (str): A single case-folded token.

    Returns:
        str: The stem, or the token unchanged when no suffix can be removed safely.
    """
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM_LENGTH:
            # The stem need not be a word. ``removes`` becomes ``remov``, which is fine: the only property
            # that matters is that the same rule runs over the query and over the block.
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    """
    Split text into stemmed tokens.

    Args:
        text (str): Any text.

    Returns:
        list[str]: Case-folded, stemmed tokens of at least :data:`MIN_TOKEN_LENGTH` characters, in order.
    """
    return [stem(token) for token in _WORD.findall(text.casefold()) if len(token) >= MIN_TOKEN_LENGTH]


def block_tokens(block: Block) -> list[str]:
    """
    The tokens a block contributes.

    Reads through :func:`boltzmann.query.scan.searchable_text`, which already knows which fields each
    kind of block carries text in -- and that a canonical block carries none, being a descriptor over
    bytes rather than prose.

    Args:
        block (Block): The block to read.

    Returns:
        list[str]: Its tokens.
    """
    return tokenize(" ".join(searchable_text(block)))


def _ranked(scores: dict[BlockId, float], limit: int) -> list[tuple[BlockId, float]]:
    """Scores as a ranking. Ties break on the identity, so two runs agree on the order."""
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0].hex))
    return ordered[:limit]


class InvertedIndex(AbstractIndex):
    """
    Term postings over a module's composition.

    Attributes:
        postings (dict[str, dict[BlockId, int]]): Term to the blocks carrying it, with term frequency.
        documents (int): How many blocks were indexed, for the idf denominator.
    """

    KIND: ClassVar[IndexKind] = IndexKind.INVERTED

    def __init__(self) -> None:
        """Build an empty index."""
        self.postings: dict[str, dict[BlockId, int]] = {}
        self.documents = 0

    def build(self, blocks: Iterable[Block]) -> None:
        """
        Populate from a composition, discarding whatever was indexed before.

        Rebuilding rather than patching is the honest operation here: a version is a set of blocks, and
        the SDK calls this after every commit with the new set.

        Args:
            blocks (Iterable[Block]): The blocks of the version being indexed.
        """
        self.postings = {}
        self.documents = 0
        for block in blocks:
            self.documents += 1
            for token in block_tokens(block):
                self.postings.setdefault(token, {}).setdefault(block.block_id, 0)
                self.postings[token][block.block_id] += 1

    def search(self, query: Any, limit: int = 10) -> list[tuple[BlockId, float]]:
        """
        Rank blocks by idf-weighted term overlap.

        A term present in few blocks counts for more than one present in most of them, which is the whole
        of what makes term matching useful. Scores are normalized by the query's total possible weight,
        so a full match is 1.0 regardless of how many terms were asked for.

        Args:
            query (Any): Text, or anything with a ``str`` form.
            limit (int): Maximum number of candidates.

        Returns:
            list[tuple[BlockId, float]]: Candidates with their score, best first.
        """
        terms = tokenize(str(query))
        if not terms or not self.documents:
            return []

        weights = {term: self._idf(term) for term in dict.fromkeys(terms)}
        total = sum(weights.values())
        if total <= 0:
            return []

        scores: dict[BlockId, float] = {}
        for term, weight in weights.items():
            for block_id in self.postings.get(term, {}):
                scores[block_id] = scores.get(block_id, 0.0) + weight / total

        return _ranked(scores, limit)

    def _idf(self, term: str) -> float:
        """Inverse document frequency, smoothed so an unseen term contributes nothing rather than
        dividing by zero."""
        frequency = len(self.postings.get(term, {}))
        if frequency == 0:
            return 0.0
        return math.log(1 + self.documents / frequency)


class VectorIndex(AbstractIndex):
    """
    Hashing bag-of-words vectors, and the only index here that travels.

    Every token lands in one of :attr:`DIMS` buckets by hash, with a sign drawn from the same hash so
    that collisions cancel instead of accumulating. The vector is L2-normalized, which makes cosine
    similarity a dot product.

    Attributes:
        vectors (dict[BlockId, list[float]]): One unit vector per indexed block.
    """

    KIND: ClassVar[IndexKind] = IndexKind.VECTOR
    REBUILDABLE: ClassVar[bool] = False

    MODEL_TAG: ClassVar[str] = "sandbox-hashing-bow/2"
    """What produced these vectors. A consumer refuses an index built by anything else, because vectors
    from two models occupy different spaces and comparing them means nothing.

    Bumped to ``/2`` when stemming entered the tokenizer. Nothing about the arithmetic changed, but the
    tokens it hashes did, so a vector built by ``/1`` sits somewhere else in the same 256 dimensions --
    which is exactly the case this tag exists to refuse. Anything that changes what gets hashed, or how,
    changes the model.
    """

    DIMS: ClassVar[int] = 256

    PRECISION: ClassVar[int] = 6
    """Decimals kept in a vector.

    Rounding is what makes the serialized index byte-identical across platforms, and therefore its layer
    digest reproducible. It is applied when the vector is *built*, not only when it is dumped, so that a
    consumer who loaded the index holds exactly what the publisher holds -- round only on the way out and
    the two ends rank with different numbers, which is a disagreement waiting for a near-tie.
    """

    def __init__(self) -> None:
        """Build an empty index."""
        self.vectors: dict[BlockId, list[float]] = {}

    @property
    def model_tag(self) -> str | None:
        """The model behind these vectors."""
        return self.MODEL_TAG

    def build(self, blocks: Iterable[Block]) -> None:
        """
        Embed every block, discarding whatever was indexed before.

        Args:
            blocks (Iterable[Block]): The blocks of the version being indexed.
        """
        self.vectors = {}
        for block in blocks:
            tokens = block_tokens(block)
            if tokens:
                self.vectors[block.block_id] = self._embed(tokens)

    def search(self, query: Any, limit: int = 10) -> list[tuple[BlockId, float]]:
        """
        Rank blocks by cosine similarity to the query.

        Args:
            query (Any): Text, or anything with a ``str`` form.
            limit (int): Maximum number of candidates.

        Returns:
            list[tuple[BlockId, float]]: Candidates with their similarity, best first. Non-positive
            similarities are dropped: they are not weak matches, they are hash collisions.
        """
        tokens = tokenize(str(query))
        if not tokens or not self.vectors:
            return []

        probe = self._embed(tokens)
        scores = {
            block_id: similarity
            for block_id, vector in self.vectors.items()
            if (similarity := sum(a * b for a, b in zip(probe, vector, strict=True))) > 0
        }
        return _ranked(scores, limit)

    def dump(self) -> bytes:
        """
        Serialize so the index can travel inside its module's layer.

        Returns:
            bytes: Canonical JSON -- sorted keys, rounded values, no incidental whitespace -- so two
            clients that indexed the same blocks publish the same bytes under the same digest.
        """
        document = {
            "model_tag": self.MODEL_TAG,
            "dims": self.DIMS,
            "vectors": {
                str(block_id): vector for block_id, vector in sorted(self.vectors.items(), key=lambda pair: pair[0].hex)
            },
        }
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    def load(self, data: bytes) -> None:
        """
        Restore an index a peer published.

        Args:
            data (bytes): What :meth:`dump` produced.

        Raises:
            IndexFormatError: If the bytes are not a dump of this shape, or were produced by a different
                model or dimensionality. The SDK checks the model tag on the layer's annotation too; this
                is the same refusal enforced against the payload itself, for a caller that loads bytes
                without a manifest in hand.
        """
        try:
            document = json.loads(data)
            model_tag = document["model_tag"]
            dims = int(document["dims"])
            vectors = document["vectors"]
        except (ValueError, KeyError, TypeError) as error:
            raise IndexFormatError(f"not a {self.KIND.value} index dump: {error}") from error

        if model_tag != self.MODEL_TAG:
            raise IndexFormatError(
                f"this index was built by {model_tag!r} but is being loaded into {self.MODEL_TAG!r}; "
                f"vectors from two models occupy different spaces, so the ranking would be meaningless"
            )
        if dims != self.DIMS:
            raise IndexFormatError(f"this index has {dims} dimensions, expected {self.DIMS}")

        self.vectors = {
            BlockId.parse(block_id): [float(value) for value in vector] for block_id, vector in vectors.items()
        }

    def _embed(self, tokens: list[str]) -> list[float]:
        """A unit vector for a bag of tokens, by signed feature hashing."""
        buckets = [0.0] * self.DIMS
        for token in tokens:
            digest = hashlib.sha256(token.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.DIMS
            # The sign comes from a byte the bucket did not consume, so it is independent of the bucket.
            buckets[bucket] += 1.0 if digest[4] & 1 else -1.0

        norm = math.sqrt(sum(value * value for value in buckets))
        if norm == 0:
            # Every token cancelled out. Rare, and a zero vector matches nothing, which is correct.
            return buckets
        return [round(value / norm, self.PRECISION) for value in buckets]
