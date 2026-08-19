"""Reconciliation: what the refusal to overwrite a diverged remote was standing in for (paper Section 12).

Detecting divergence is a reader's duty and belongs to the distribution path, which refuses rather than
overwrites. Refusing is safe and incomplete: it leaves reconciliation to hand-editing, and it means a
partial install can never be published back over the tag it came from. This package closes both.

Because a snapshot names its parents as a list, a reconciliation is representable without a new document;
because a module composition is a set of immutable blocks, most of one is mechanical; and because the
result is then put through the ingestion gate, the conflicts that remain surface as verdicts rather than
as an unresolvable state.
"""

from boltzmann.reconcile.ancestry import (
    common_ancestor,
    composition_at,
    is_reopenable,
    snapshot_at,
    snapshots_between,
)
from boltzmann.reconcile.gate import (
    BlockVerdict,
    IncomingReport,
    MissingEvidence,
    judge_incoming,
)
from boltzmann.reconcile.merge import ModuleReconciliation, merge_module, reconciled_modules
from boltzmann.reconcile.requests import (
    AttributionReport,
    ReconcilePlan,
    ReconcileRequest,
    ReconcileResult,
    ReconcileStrategy,
)
from boltzmann.reconcile.resolution import (
    ReconcileState,
    ReconcileStatus,
    RemovalAcceptance,
    Resolution,
    ResolutionKind,
)
from boltzmann.reconcile.strategies import attribution_for, attribution_table, merged_parents, replay_steps

__all__ = [
    "AttributionReport",
    "BlockVerdict",
    "IncomingReport",
    "MissingEvidence",
    "ModuleReconciliation",
    "ReconcilePlan",
    "ReconcileRequest",
    "ReconcileResult",
    "ReconcileStrategy",
    "RemovalAcceptance",
    "ResolutionKind",
    "Resolution",
    "ReconcileStatus",
    "ReconcileState",
    "attribution_for",
    "attribution_table",
    "common_ancestor",
    "composition_at",
    "is_reopenable",
    "judge_incoming",
    "merge_module",
    "merged_parents",
    "reconciled_modules",
    "replay_steps",
    "snapshot_at",
    "snapshots_between",
]
