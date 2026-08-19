"""What each strategy records, given that all three compute the same thing.

Everything about the reconciled *content* lives in :mod:`boltzmann.reconcile.merge`, and everything about
judging it in :mod:`boltzmann.reconcile.gate`. What is left -- and it is all that separates merge, rebase
and squash -- is the lineage written down (paper Section 12.3, Table 3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from boltzmann.reconcile.requests import AttributionReport, ReconcileStrategy

if TYPE_CHECKING:
    from boltzmann.identity.digest import OciDigest
    from boltzmann.module.snapshot import Snapshot


def attribution_for(strategy: ReconcileStrategy, collapsed: int, replayable: int | None = None) -> AttributionReport:
    """
    What one strategy costs, for a contribution of a given size.

    Args:
        strategy (ReconcileStrategy): The strategy.
        collapsed (int): How many snapshots the other history added on top of the ancestor.
        replayable (int | None): How many of those can be restated here, which is how many snapshots a
            rebase writes. Fewer than ``collapsed`` means the artifact did not carry those versions'
            compositions, so a rebase unavoidably collapses some of them -- an effect the operator asked
            for from a squash and not from a rebase, which is why it is reported rather than absorbed.
            Defaults to all of them.

    Returns:
        AttributionReport: The consequence of choosing it.
    """
    replayed = max(collapsed if replayable is None else replayable, 1)
    if strategy is ReconcileStrategy.MERGE:
        return AttributionReport(
            strategy=strategy,
            parents=2,
            snapshots_written=1,
            keeps_their_snapshots=True,
            mints_new_identities=False,
            their_signatures_survive=True,
        )
    if strategy is ReconcileStrategy.REBASE:
        return AttributionReport(
            strategy=strategy,
            parents=1,
            snapshots_written=replayed,
            keeps_their_snapshots=False,
            mints_new_identities=True,
            their_signatures_survive=False,
        )
    return AttributionReport(
        strategy=strategy,
        parents=1,
        snapshots_written=1,
        keeps_their_snapshots=False,
        mints_new_identities=True,
        their_signatures_survive=False,
    )


def attribution_table(collapsed: int, replayable: int | None = None) -> dict[ReconcileStrategy, AttributionReport]:
    """
    Every strategy's consequence, so a choice can be made side by side.

    Args:
        collapsed (int): How many snapshots the other history added on top of the ancestor.
        replayable (int | None): How many of those a rebase could restate.

    Returns:
        dict[ReconcileStrategy, AttributionReport]: One report per strategy.
    """
    return {strategy: attribution_for(strategy, collapsed, replayable) for strategy in ReconcileStrategy}


def replay_steps(strategy: ReconcileStrategy, chain: list[Snapshot]) -> list[Snapshot]:
    """
    Which versions of the other history the result is computed against, in order.

    A rebase writes one snapshot per snapshot it replays, so it walks the chain and each step reconciles
    against that intermediate version -- which is what makes the replay deterministic here: the step's
    result is Equation 1 against the state that step had, not a patch applied to a tree. Merge and squash
    write one snapshot, so they are computed against the head alone.

    Args:
        strategy (ReconcileStrategy): The chosen strategy.
        chain (list[Snapshot]): The other history's snapshots above the ancestor, oldest first, restricted
            to the versions that can be reopened here -- see
            :func:`~boltzmann.reconcile.ancestry.is_reopenable`.

    Returns:
        list[Snapshot]: The versions to reconcile against, in order. The last is always the head, so every
        strategy ends at the same composition.
    """
    if not chain:
        return []
    if strategy is ReconcileStrategy.REBASE:
        return list(chain)
    return [chain[-1]]


def merged_parents(strategy: ReconcileStrategy, theirs: OciDigest) -> list[OciDigest]:
    """
    Which histories the resulting snapshot records as merged-in parents.

    Only a merge records one. That single difference is why only a merge keeps the other side's snapshots
    in the history, and therefore why only a merge keeps their signature covering something.

    Args:
        strategy (ReconcileStrategy): The chosen strategy.
        theirs (OciDigest): The other history's head.

    Returns:
        list[OciDigest]: The additional parents, which is ``[theirs]`` for a merge and empty otherwise.
    """
    return [theirs] if strategy is ReconcileStrategy.MERGE else []
