"""Signing scopes: what a key is authorized to do (paper Section 8.5).

A scope names something the protocol already defines rather than a generic role, so that a
consumer can check a claim mechanically. The required scope set of a snapshot is computed from
its difference against its first parent -- never taken from what a signature claims -- which is
what turns "dropping canonical evidence is allowed only under explicit policy" from a statement
a consumer cannot confirm into a checkable one about who was permitted to do it.
"""

from __future__ import annotations

from enum import StrEnum


class Scope(StrEnum):
    """One thing a key may be authorized to do, named after the operation it authorizes."""

    INGEST = "ingest"
    """Preserving a source as canonical evidence. Required when the canonical composition gained blocks."""

    COMMIT = "commit"
    """Adding derived blocks; ordinary drops, supersession, and demotion. Required when a
    non-canonical composition changed."""

    DROP_CANONICAL = "drop:canonical"
    """Excluding canonical evidence, with its cascade. Required when the canonical composition lost
    blocks. Its own scope rather than part of ``commit`` because the asymmetry is Principle 2's: an
    ordinary drop is recoverable by a later commit, whereas excluding canonical evidence forfeits
    re-derivation from that source."""

    REDACT = "redact"
    """Destroying bytes a retained root still names. Required when blocks became tombstoned."""

    GOVERN = "govern"
    """Revising the trust root. Required when the trust root digest changed."""

    PROPOSE = "propose"
    """Offering a snapshot for someone else's consideration. Never sufficient for a published head
    (paper Section 12.6); never *required* by any snapshot -- it is only ever held."""


PROPOSABLE_SCOPES = frozenset({Scope.INGEST, Scope.COMMIT})
"""The scopes a ``propose``-holding key may stand in for.

The paper leaves this implicit and the two readings conflict: taken literally, ``propose`` is
never in a computed requirement, so a propose-only key would fail every snapshot with an
insufficient scope -- contradicting Section 12.6, which calls such snapshots "attributable,
verifiable, and explicitly not the published head". Resolution: ``propose`` satisfies content
scopes only. A "proposal" that destroys canonical evidence, redacts bytes, or rewrites authority
is not a proposal, so ``drop:canonical``, ``redact``, and ``govern`` are never proposable.
"""
