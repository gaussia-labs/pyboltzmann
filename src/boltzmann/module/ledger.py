"""A read-only view over the provenance module.

The ledger answers what must be recomputed when a source changes (paper Section 5), which means both
the query path and the retention path need to read it: a query has to know what a newer block
superseded, and a drop has to know what cited the evidence it is about to exclude.

Reading it means decoding every provenance block, so it is built once and passed around rather than
re-walked per question. It lives here, in the module layer, because both callers already depend on
modules and neither should have to depend on the other.

Two reverse indices are the point of it. ``dependents`` inverts ``derived_from`` -- the ledger records
which evidence a block cites, and a cascade needs the opposite direction. ``superseded_by`` inverts
supersession the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    DemotionRecord,
    DerivationRecord,
    ProvenanceBlock,
    RemovalRecord,
    SupersessionRecord,
)

if TYPE_CHECKING:
    from boltzmann.blocks.provenance import Producer
    from boltzmann.identity.digest import BlockId
    from boltzmann.module.module import Module


@dataclass
class Ledger:
    """
    What the provenance module says about the blocks in a snapshot.

    Attributes:
        locators (dict[BlockId, str]): Where in its source each derived block came from.
        superseded_by (dict[BlockId, BlockId]): Blocks a newer one replaced, and by which.
        competing (dict[BlockId, set[BlockId]]): Blocks that more than one successor claims to supersede.
            Two histories can each supersede the same block with a different successor, and both edges are
            legitimately recorded -- the conflict is a precedence question, not an admissibility one. It
            lives here rather than being flattened into ``superseded_by``, whose last writer would otherwise
            win in silence (paper Section 12.4).
        demoted (set[BlockId]): Blocks whose retrieval priority was lowered. Still members, still
            verifiable -- only less accessible (paper Section 10.4).
        evidence (dict[BlockId, list[BlockId]]): What each derived block cites, as recorded.
        dependents (dict[BlockId, set[BlockId]]): The inverse: which derived blocks cite a given block.
            This is the edge a cascade walks.
        producers (dict[BlockId, Producer]): What produced each derived block, so a drop can be stated
            over everything one model version made.
        derivation_records (dict[BlockId, BlockId]): The provenance block that records each derivation,
            so a cascade can report which edges it touches.
        removed (set[BlockId]): Blocks a recorded removal already excluded.
    """

    locators: dict[BlockId, str] = field(default_factory=dict)
    superseded_by: dict[BlockId, BlockId] = field(default_factory=dict)
    competing: dict[BlockId, set[BlockId]] = field(default_factory=dict)
    demoted: set[BlockId] = field(default_factory=set)
    evidence: dict[BlockId, list[BlockId]] = field(default_factory=dict)
    dependents: dict[BlockId, set[BlockId]] = field(default_factory=dict)
    producers: dict[BlockId, Producer] = field(default_factory=dict)
    derivation_records: dict[BlockId, BlockId] = field(default_factory=dict)
    removed: set[BlockId] = field(default_factory=set)

    @classmethod
    def of(cls, modules: dict[MemoryType, Module]) -> Ledger:
        """
        Read the ledger.

        Args:
            modules (dict[MemoryType, Module]): The installed modules. A brain with no provenance module
                yields an empty ledger rather than an error, because a partial install is legitimate --
                it just cannot answer these questions.

        Returns:
            Ledger: The view.
        """
        ledger = cls()
        provenance = modules.get(MemoryType.PROVENANCE)
        if provenance is None:
            return ledger

        for block_id in provenance.block_ids:
            if not provenance.store.is_resolvable(block_id):
                continue
            entry = provenance.get(block_id)
            if isinstance(entry, ProvenanceBlock):
                ledger._absorb(entry, block_id)
        return ledger

    def _absorb(self, entry: ProvenanceBlock, entry_id: BlockId) -> None:
        record = entry.record

        if isinstance(record, DerivationRecord):
            if record.locator is not None:
                self.locators[record.block] = record.locator
            self.evidence[record.block] = list(record.derived_from)
            self.producers[record.block] = record.producer
            self.derivation_records[record.block] = entry_id
            for cited in record.derived_from:
                self.dependents.setdefault(cited, set()).add(record.block)

        elif isinstance(record, SupersessionRecord):
            # Recorded before the assignment, so the first successor is compared against rather than
            # overwritten: whichever arrives second is what makes the precedence ambiguous, and the answer
            # must not depend on which one that was.
            held = self.superseded_by.get(record.supersedes)
            if held is not None and held != record.block:
                self.competing.setdefault(record.supersedes, {held}).add(record.block)
            self.superseded_by[record.supersedes] = record.block

        elif isinstance(record, DemotionRecord):
            self.demoted.add(record.block)

        elif isinstance(record, RemovalRecord):
            self.removed.update(record.blocks)

    # --- Queries --------------------------------------------------------------

    def closure(self, origin: BlockId) -> set[BlockId]:
        """
        Every block that cites ``origin``, transitively.

        Args:
            origin (BlockId): The block whose dependents are wanted.

        Returns:
            set[BlockId]: The dependents. Excludes ``origin`` itself, and terminates on cycles.
        """
        found: set[BlockId] = set()
        frontier = {origin}
        while frontier:
            discovered = {
                dependent
                for block_id in frontier
                for dependent in self.dependents.get(block_id, set())
                if dependent not in found and dependent != origin
            }
            if not discovered:
                break
            found |= discovered
            frontier = discovered
        return found

    def successors_of(self, block_id: BlockId) -> set[BlockId]:
        """
        Every block recorded as superseding one block.

        Ordinarily one, or none. More than one means two histories each replaced it with something
        different, which is a precedence question a reconciliation has to settle rather than absorb.

        Args:
            block_id (BlockId): The block being superseded.

        Returns:
            set[BlockId]: Its recorded successors.
        """
        contested = self.competing.get(block_id)
        if contested is not None:
            return set(contested)
        held = self.superseded_by.get(block_id)
        return {held} if held is not None else set()

    def contested(self, block_id: BlockId) -> set[BlockId]:
        """The successors of a block whose precedence is still open.

        Two histories replacing the same block with different successors leaves two edges, and both stay
        recorded -- the record of what happened is not what gets resolved. What resolves is the precedence,
        and the only way this architecture states precedence is a supersession edge, so a tie broken in favour
        of one successor appears here as the other one being superseded in turn.

        A settled question therefore returns nothing, which is what makes it possible to ask twice.

        Args:
            block_id (BlockId): The block being superseded.

        Returns:
            set[BlockId]: The successors still competing, or empty when there is one answer or none.
        """
        successors = self.successors_of(block_id)
        if len(successors) < 2:
            return set()
        standing = {block for block in successors if self.superseded_by.get(block) not in successors}
        return standing if len(standing) > 1 else set()

    def made_by(self, producer: Producer) -> set[BlockId]:
        """
        Every derived block a producer made.

        A producer with no version matches any version of the same identity, so "everything that model
        ever derived" and "everything version 2026-07 derived" are both expressible.

        Args:
            producer (Producer): Whose output to find.

        Returns:
            set[BlockId]: The blocks it produced.
        """
        return {
            block_id
            for block_id, recorded in self.producers.items()
            if recorded.kind is producer.kind
            and recorded.id == producer.id
            and (producer.version is None or recorded.version == producer.version)
        }

    def is_accessible(self, block_id: BlockId) -> bool:
        """
        Whether a block should surface in retrieval by default.

        Supersession and demotion change accessibility, not membership: the block is still in the
        composition and still proves into the root.

        Args:
            block_id (BlockId): The block to check.

        Returns:
            bool: Whether it is neither superseded nor demoted.
        """
        return block_id not in self.superseded_by and block_id not in self.demoted
