"""Retention: explicit, auditable, and never silent.

The operations are declared on :class:`~boltzmann.protocol.operations.BrainRetention` and implemented on
:class:`~boltzmann.brain.Brain`. The cascade and the reachability walk live here because both are pure
functions over the installed modules: a plan can be produced, and a sweep computed, before anything is
written.
"""

from boltzmann.retention.cascade import plan_cascade, plan_many, structural_dependents
from boltzmann.retention.policy import DEFAULT_RETAINED_ROOTS, PERMISSIVE_POLICY, RetentionPolicy
from boltzmann.retention.reachability import mark, reachable_from, sweep
from boltzmann.retention.requests import (
    CascadePlan,
    DropRequest,
    DropResult,
    ProducerDropRequest,
    PruneReport,
    RedactionResult,
    ResolvabilityReport,
    SupersessionResult,
)

__all__ = [
    "DEFAULT_RETAINED_ROOTS",
    "PERMISSIVE_POLICY",
    "CascadePlan",
    "DropRequest",
    "DropResult",
    "ProducerDropRequest",
    "PruneReport",
    "RedactionResult",
    "ResolvabilityReport",
    "RetentionPolicy",
    "SupersessionResult",
    "mark",
    "plan_cascade",
    "plan_many",
    "reachable_from",
    "structural_dependents",
    "sweep",
]
