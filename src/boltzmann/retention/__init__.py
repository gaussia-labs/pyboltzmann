"""Retention types and policy: explicit, auditable, and never silent.

The operations are declared on :class:`~boltzmann.protocol.operations.BrainRetention`.
"""

from boltzmann.retention.policy import DEFAULT_RETAINED_ROOTS, PERMISSIVE_POLICY, RetentionPolicy
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
]
