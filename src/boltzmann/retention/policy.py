"""Retention policy is configuration, not protocol.

As with query planning, the protocol fixes the operations and the invariants, not the
thresholds. Who may drop from which module, how deep a provenance cascade runs before
requiring review, how many roots to retain, and which categories of content are redactable
are deployment decisions (paper Section 10.7).

What the protocol *does* require is Principle 8: every drop, supersession, prune, and
redaction must be explicit, recorded in provenance, and reportable. So a policy can widen
or narrow what is permitted, but :attr:`RetentionPolicy.record_removals` has no ``False``:
there is no configuration under which forgetting goes unrecorded.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import RemovalMechanism
from boltzmann.exceptions import RetentionPolicyError

DEFAULT_RETAINED_ROOTS = 10
"""How many recent snapshots a brain keeps reachable, on top of tagged releases."""


class RetentionPolicy(BaseModel):
    """
    The deployment's answers to the questions the protocol leaves open.

    Attributes:
        droppable_modules (list[MemoryType]): Which modules permit ``drop``. The episodic
            module can never be listed: it is append-only by protocol, not by policy.
        canonical_drop_allowed (bool): Whether canonical evidence may be excluded at all.
            Dropping it is privileged because it forfeits re-derivation from that source
            (Principle 2).
        cascade_review_threshold (int | None): How many dependents a cascade may drop
            before the commit requires human review. ``None`` means never require review.
        retained_roots (int): How many recent snapshots stay reachable for pruning
            purposes.
        redactable_media_types (list[str] | None): Which content may have its bytes
            destroyed. ``None`` forbids redaction entirely, which is the safe default:
            redaction is for law and safety, not for cleanup.
        allowed_mechanisms (list[RemovalMechanism] | None): Restrict removals to a subset
            of mechanisms. ``None`` permits every mechanism the module allows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    droppable_modules: list[MemoryType] = Field(
        default_factory=lambda: [
            MemoryType.CANONICAL,
            MemoryType.SEMANTIC,
            MemoryType.PROCEDURAL,
            MemoryType.PROVENANCE,
        ]
    )
    canonical_drop_allowed: bool = False
    cascade_review_threshold: int | None = None
    retained_roots: int = Field(default=DEFAULT_RETAINED_ROOTS, ge=1)
    redactable_media_types: list[str] | None = None
    allowed_mechanisms: list[RemovalMechanism] | None = None

    @property
    def record_removals(self) -> bool:
        """
        Whether every removal is recorded in provenance. Always ``True``.

        Exposed as a property rather than a field so that no configuration, and no
        deserialized document, can turn auditability off.
        """
        return True

    def authorize(self, mechanism: RemovalMechanism, memory_type: MemoryType) -> None:
        """
        Check that a removal is permitted, raising if it is not.

        Args:
            mechanism (RemovalMechanism): How the removal would happen.
            memory_type (MemoryType): Which module it would touch.

        Raises:
            RetentionPolicyError: If the policy or the protocol forbids it.
        """
        if mechanism is RemovalMechanism.DROP and memory_type.is_append_only:
            raise RetentionPolicyError(
                f"the {memory_type.value} module is append-only by protocol: no policy can permit a drop"
            )
        if self.allowed_mechanisms is not None and mechanism not in self.allowed_mechanisms:
            permitted = ", ".join(sorted(item.value for item in self.allowed_mechanisms))
            raise RetentionPolicyError(f"policy permits only: {permitted}; {mechanism.value} was requested")
        if mechanism is RemovalMechanism.DROP and memory_type not in self.droppable_modules:
            raise RetentionPolicyError(f"policy does not permit dropping from the {memory_type.value} module")
        if (
            mechanism is RemovalMechanism.DROP
            and memory_type is MemoryType.CANONICAL
            and not self.canonical_drop_allowed
        ):
            raise RetentionPolicyError(
                "dropping canonical evidence is privileged and this policy does not allow it: "
                "excluding a source forfeits re-derivation from it"
            )
        if mechanism.is_redaction and not self.redactable_media_types:
            raise RetentionPolicyError(
                f"policy declares no redactable media types, so {mechanism.value} is not permitted; "
                f"wrong or obsolete knowledge is dropped, not redacted"
            )

    def requires_review(self, cascade_size: int) -> bool:
        """
        Whether a cascade of this size needs human review before it commits.

        Args:
            cascade_size (int): How many dependents the cascade would drop.

        Returns:
            bool: Whether review is required.
        """
        return self.cascade_review_threshold is not None and cascade_size > self.cascade_review_threshold


PERMISSIVE_POLICY = RetentionPolicy(canonical_drop_allowed=True)
"""A policy for development and tests: canonical drops allowed, redaction still refused."""
