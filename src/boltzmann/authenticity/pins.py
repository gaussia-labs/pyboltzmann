"""The trust pin: the one thing that comes from outside (paper Section 8.8).

The first trust root is self-signed, and read from inside the artifact it proves nothing: the
entity asserting a key is authorized is that key. Trust cannot be manufactured from inside a
system -- what a design can do is reduce the exposure to a single decision, taken once. The pin
is that decision: the digest of a trust root, recorded in consumer-side state, compared on every
verification.

It lives in a store pointer and **never in the artifact**: a pin an artifact could supply would
be a pin the attacker supplies. One pin per store, because a ``Brain`` is a handle on one brain;
a client managing many brains keeps its own pin table.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from boltzmann.constants import PROTOCOL_VERSION
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.serialization import canonicalize, parse_json_strict
from boltzmann.identity.time import Timestamp, utc_timestamp
from boltzmann.store.base import BlockStore

PIN_POINTER = "trust"
"""The mutable pointer holding this store's trust pin."""


class PinSource(StrEnum):
    """How a pin was established. Recorded because the two carry different weight."""

    FIRST_USE = "first_use"
    """Trust on first use, then pin: the ``known_hosts`` model. Converts an ongoing exposure
    into a single moment of exposure; does not prove the first contact was legitimate."""

    OUT_OF_BAND = "out_of_band"
    """Compared by hand against a digest published through an independent channel. Compromising
    the distribution then requires compromising two independent things."""


class TrustPin(BaseModel):
    """
    The anchor a consumer holds for this brain.

    Attributes:
        boltzmann (int): Protocol version that wrote the pin.
        trust_root (OciDigest): The pinned trust-root digest.
        source (PinSource): How the pin was established.
        pinned_at (Timestamp): When. Descriptive, like every timestamp here: no decision reads it.
        genesis (OciDigest | None): The genesis snapshot at pin time, so a brain that moved
            repositories can still be recognized as the same brain.
        reference (str | None): The origin repository at pin time, for diagnostics.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = PROTOCOL_VERSION
    trust_root: OciDigest
    source: PinSource
    pinned_at: Timestamp
    genesis: OciDigest | None = None
    reference: str | None = None


def read_pin(store: BlockStore) -> TrustPin | None:
    """
    Read this store's trust pin.

    Args:
        store (BlockStore): The store holding the pointer.

    Returns:
        TrustPin | None: The pin, or ``None`` when no anchor was ever recorded.
    """
    raw = store.read_pointer(PIN_POINTER)
    return TrustPin.model_validate(parse_json_strict(raw)) if raw else None


def write_pin(
    store: BlockStore,
    trust_root: OciDigest,
    source: PinSource,
    genesis: OciDigest | None = None,
    reference: str | None = None,
) -> TrustPin:
    """
    Record a trust-root digest as this brain's anchor.

    Overwriting an existing pin is deliberate and allowed -- re-anchoring is the consumer's
    decision -- but it is a decision, so callers surface it rather than doing it silently.

    Args:
        store (BlockStore): The store holding the pointer.
        trust_root (OciDigest): The digest to pin.
        source (PinSource): How this pin was established.
        genesis (OciDigest | None): The genesis snapshot at pin time.
        reference (str | None): The origin repository at pin time.

    Returns:
        TrustPin: The recorded pin.
    """
    pin = TrustPin(
        trust_root=trust_root,
        source=source,
        pinned_at=utc_timestamp(),
        genesis=genesis,
        reference=reference,
    )
    store.write_pointer(PIN_POINTER, canonicalize(pin.model_dump(mode="json", exclude_none=True)))
    return pin
