"""The verification policy: the deployment's appetite for risk, never its honesty.

As with query planning and retention, the protocol fixes the checks and the reporting, not the
deployment's tolerances (paper Section 8.10). A policy states whether an unsigned brain may be
installed, how many valid signatures a head must carry, and whether a ``propose``-scoped head
may be treated as the current state. What is *not* configurable is whether the result is
reported: no policy can present an unverified brain as verified, which is why the report's state
is a derived property no configuration touches.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class UnsignedPolicy(StrEnum):
    """What an installation does with a brain that carries no signature at all."""

    WARN = "warn"
    """Permit and say so. The paper's default for a first pull: unsigned is the legitimate
    zero-configuration case, and refusing it by default would punish every brain that never
    claimed authorship."""

    REFUSE = "refuse"
    """Do not install. The paper's default for a brain previously seen signed, where a missing
    signature is evidence of stripping rather than of never having signed."""

    PERMIT = "permit"
    """Permit silently. For deployments that decided integrity alone is their bar."""


class VerificationPolicy(BaseModel):
    """
    A consumer's verification tolerances.

    Attributes:
        unsigned (UnsignedPolicy): Whether an unsigned brain may be installed. The stricter
            previously-seen-signed rule is applied by the installer, which is the one that knows
            what was previously seen.
        required_signatures (int): How many valid signatures from distinct keys a head must
            carry to be authorized. Distinct *keys*, never records: the same key can produce two
            different valid records over one snapshot by switching hash algorithm.
        allow_propose_head (bool): Whether a snapshot whose only valid signatures are
            ``propose``-scoped may be treated as the brain's current state. Off by default: such
            a snapshot is attributable and verifiable and explicitly not the published head
            (paper Section 12.6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unsigned: UnsignedPolicy = UnsignedPolicy.WARN
    required_signatures: int = Field(default=1, ge=1)
    allow_propose_head: bool = False
