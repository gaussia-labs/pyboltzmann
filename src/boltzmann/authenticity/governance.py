"""The shapes governance operations exchange when a quorum spans machines.

A trust-root revision with a quorum of two or more is signed by parties who may not share a
machine. The revision document is built **once** -- it carries ``created_at``, so two independent
constructions would produce different bytes and signatures over different digests -- and that
exact file travels by any channel: nothing in it is secret, and each party inspects what it
signs. These models are the file's escort: what a planner hands out, and what a completed
rotation reports.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from boltzmann.authenticity.record import SignatureRecord
from boltzmann.identity.digest import OciDigest


class RotationPlan(BaseModel):
    """
    A trust-root revision built and not yet in force.

    Attributes:
        document (bytes): The revision snapshot's canonical bytes -- the exact bytes every
            signature must cover. This is what travels to a countersigner.
        digest (OciDigest): The document's identity, which every collected record must name.
        quorum_required (int): How many distinct ``govern`` holders of the revision in force
            must sign before the head may advance.
        eligible (tuple[str, ...]): The fingerprints that can satisfy it: the active ``govern``
            holders of the revision in force.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    document: bytes
    digest: OciDigest
    quorum_required: int = Field(ge=1)
    eligible: tuple[str, ...]


class RotationResult(BaseModel):
    """
    A governance act that took effect.

    Attributes:
        snapshot (OciDigest): The revision snapshot now at the head.
        revision (int): The trust root revision now in force.
        quorum_required (int): What the previous revision demanded.
        quorum_met (int): Distinct qualifying keys that signed.
        records (tuple[SignatureRecord, ...]): The signatures that carried it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: OciDigest
    revision: int = Field(ge=1)
    quorum_required: int = Field(ge=1)
    quorum_met: int = Field(ge=0)
    records: tuple[SignatureRecord, ...]
