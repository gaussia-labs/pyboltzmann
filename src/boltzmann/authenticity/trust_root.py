"""The trust root: which keys are authorized to sign for a brain (paper Section 8.5).

A signature answers *who*; the trust root answers *and why should that matter*. It travels
inside the snapshot document rather than in a module or a layer, because a consumer pulling from
a mirror, a cache, or a tarball must be able to learn the authorized keys without reaching a
server -- and because carrying it inside the signed bytes means a signature can never be
evaluated against a key list the signer did not commit to.

The trust root is not access control. Nothing stops someone modifying a copy of a brain they
hold; the trust root is a rule the *reader* applies. It does not prevent writing, it prevents
impersonation.

Two forms of position appear here, and they are not interchangeable. ``since`` and
``retired_from`` name a *revision number*, because a revision travels inside the snapshot that
introduces it and naming that snapshot by digest from within the trust root would be circular.
``compromised_from`` names a *snapshot digest*, because a compromise always points at a position
already published -- and because compromise is discovered after the fact, mid-span, where no
revision boundary falls.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from boltzmann.authenticity.keys import SshPublicKey
from boltzmann.authenticity.scopes import Scope
from boltzmann.constants import PROTOCOL_VERSION, SNAPSHOT_NAMESPACE
from boltzmann.identity.digest import OciDigest
from boltzmann.identity.principal import parse_actor_id
from boltzmann.identity.serialization import canonicalize


class TrustedKey(BaseModel):
    """
    One authorized key, its scopes, and its positional validity.

    Attributes:
        key (SshPublicKey): The public key, in the canonical two-field authorized_keys form. A
            key with a comment or an options prefix is rejected outright: two spellings of one
            key would be two different trust-root digests, and a pin has to mean something.
        subject (str | None): Who holds the key, as an actor identifier. This is what finally
            connects the two halves of the paper's own promise -- that a signature is what turns a
            declared actor into an authenticated identity -- because without it a snapshot's
            provenance names a person, its signature names a fingerprint, and nothing asserts the
            two are the same.

            The claim it makes is narrow on purpose. A subject is asserted by *this brain's*
            governance and nowhere else: changing one changes the trust root, which is a revision,
            which needs a quorum evaluated against the previous revision. It is as trustworthy as
            that quorum and no more, and it is not a certificate. The grounds for believing a key
            belongs to a particular person in the world stay outside the protocol; what a subject
            adds is that once those grounds exist, the conclusion is written where a verifier will
            read it instead of living in a maintainer's memory.

            Absent is the ordinary case for a brain that never needed it, and an absent subject is
            omitted rather than serialized as null, so a trust root that names none keeps exactly
            the digest it had before this member existed and every pin against it still holds.
        scopes (tuple[Scope, ...]): What this key is authorized to do. Order is preserved as
            authored -- the document travels as bytes and is never reconstructed -- but a scope
            listed twice is rejected, because a duplicate changes the digest while changing
            nothing.
        since (int): The revision that admitted this key. Confirmable rather than asserted: a
            verifier walking revisions sees when the key first appeared, and a claim earlier
            than the chain supports is rejected (:func:`confirm_since`).
        retired_from (int | None): The revision from which the key is no longer authorized.
            Signatures at earlier positions remain valid -- retirement can never invalidate a
            signature that was valid before it.
        compromised_from (OciDigest | None): A *snapshot* from which the key's signatures are no
            longer trusted. The only construct in the protocol that withdraws a previously valid
            signature, and a verifier reports it as such rather than as an ordinary
            authorization failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: SshPublicKey
    subject: str | None = None
    scopes: tuple[Scope, ...] = Field(min_length=1)
    since: int = Field(ge=1)
    retired_from: int | None = Field(default=None, ge=1)
    compromised_from: OciDigest | None = None

    @model_validator(mode="after")
    def _reject_incoherent_positions(self) -> Self:
        """Authoring mistakes, caught where they are made rather than where they surface.

        A scope listed twice changes the digest while changing nothing; a key retired at or before
        its own admission was never authorized at all; and a subject that is not an actor
        identifier could never be compared against one a provenance record claims, which is the
        only thing a subject is for.
        """
        if self.subject is not None:
            parse_actor_id(self.subject, field=f"subject of key {self.key.fingerprint}")
        if len(set(self.scopes)) != len(self.scopes):
            listed = ", ".join(scope.value for scope in self.scopes)
            raise ValueError(f"a key's scopes are a set; {self.key.fingerprint} lists one twice: {listed}")
        if self.retired_from is not None and self.retired_from <= self.since:
            raise ValueError(
                f"key {self.key.fingerprint} is retired from revision {self.retired_from}, at or before "
                f"its admission at revision {self.since}; a key retired no later than it was admitted "
                f"was never authorized"
            )
        return self

    @property
    def fingerprint(self) -> str:
        """The key's SHA-256 fingerprint, for reports and error messages."""
        return self.key.fingerprint

    def holds(self, scope: Scope) -> bool:
        """
        Whether this entry grants a scope.

        Positional validity is deliberately not consulted here: whether the key is retired or
        compromised at a position is the trust root's and the chain's question, not the entry's.

        Args:
            scope (Scope): The scope to check.

        Returns:
            bool: Whether the scope is listed.
        """
        return scope in self.scopes


class TrustRoot(BaseModel):
    """
    The authorized keys of a brain, at one revision.

    Its identity is the digest of its canonical serialization, and that digest -- rather than
    any individual key -- is what a consumer pins (paper Section 8.7). Changing it requires a
    quorum evaluated against the *previous* revision, which is the root-rotation rule of The
    Update Framework and the reason a key list cannot authorize itself.

    Attributes:
        boltzmann (int): Protocol version. A document claiming a later one is refused rather
            than read, because reading it would mean applying rules this client does not have to
            a decision about authorship.
        revision (int): This document's position in the sequence of trust-root revisions.
            Strictly increasing, not necessarily contiguous: ``since``-confirmability works by
            refutation over observed revisions, never by arithmetic, so gaps are harmless.
        namespace (str): The SSHSIG namespace signatures for this brain are made under.
        govern_quorum (int): How many distinct ``govern``-holding keys must sign a revision. A
            quorum of 1 already stops a stranger; 2 or more is what protects a brain against a
            single stolen legitimate key.
        keys (tuple[TrustedKey, ...]): The authorized keys. Retired and compromised entries stay
            listed -- removing one would erase the record positional validity is judged against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    boltzmann: int = Field(default=PROTOCOL_VERSION, ge=1, le=PROTOCOL_VERSION)
    revision: int = Field(ge=1)
    namespace: str = SNAPSHOT_NAMESPACE
    govern_quorum: int = Field(ge=1)
    keys: tuple[TrustedKey, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_a_root_no_verifier_could_accept(self) -> Self:
        """Four authoring mistakes are caught at construction because no conforming ``init`` or
        ``rotate`` could have produced them, and each would otherwise surface far from its cause."""
        if self.namespace != SNAPSHOT_NAMESPACE:
            raise ValueError(
                f"trust root names namespace {self.namespace!r}; this version of the protocol signs "
                f"under {SNAPSHOT_NAMESPACE!r} and a root for another namespace is not for this brain"
            )
        seen: dict[bytes, str] = {}
        for entry in self.keys:
            if entry.key.blob in seen:
                raise ValueError(f"trust root lists {entry.fingerprint} more than once")
            seen[entry.key.blob] = entry.fingerprint
        if not any(entry.holds(Scope.GOVERN) for entry in self.keys):
            raise ValueError(
                "a trust root with no govern holder could never be revised: no conforming init or "
                "rotate produces one, so this document is malformed rather than merely strict"
            )
        for entry in self.keys:
            if entry.since > self.revision:
                raise ValueError(
                    f"key {entry.fingerprint} claims admission at revision {entry.since}, after this "
                    f"document's own revision {self.revision}"
                )
            if entry.retired_from is not None and entry.retired_from > self.revision:
                raise ValueError(
                    f"key {entry.fingerprint} is retired from revision {entry.retired_from}, which this "
                    f"document at revision {self.revision} cannot yet know"
                )
        return self

    # --- Identity -------------------------------------------------------------

    def canonical_bytes(self) -> bytes:
        """
        The trust root as canonical bytes.

        The same serialization the snapshot embeds it under, so the digest of the standalone
        document equals the digest of the sub-object a consumer reads out of a snapshot.

        Returns:
            bytes: The canonically serialized document.
        """
        return canonicalize(self.model_dump(mode="json", exclude_none=True))

    @property
    def digest(self) -> OciDigest:
        """The identity a consumer pins, and the value whose change makes a snapshot a revision."""
        return OciDigest.of(self.canonical_bytes())

    # --- Positional queries -----------------------------------------------------

    def entry_for(self, key: SshPublicKey) -> TrustedKey | None:
        """
        The entry listing a key, matched on the full blob.

        Never matched on a fingerprint: a verifier must not decide on a 32-byte hash of the
        thing it is deciding about.

        Args:
            key (SshPublicKey): The key to look up -- ordinarily the one embedded in a signature.

        Returns:
            TrustedKey | None: The entry, or ``None`` if the key is not listed.
        """
        for entry in self.keys:
            if entry.key.matches(key):
                return entry
        return None

    def is_retired(self, entry: TrustedKey) -> bool:
        """
        Whether an entry is retired at this revision.

        Args:
            entry (TrustedKey): The entry to check.

        Returns:
            bool: Whether ``retired_from`` has taken effect here.
        """
        return entry.retired_from is not None and self.revision >= entry.retired_from

    def holders(self, scope: Scope) -> tuple[TrustedKey, ...]:
        """
        The entries granting a scope and not retired at this revision.

        Args:
            scope (Scope): The scope to look for.

        Returns:
            tuple[TrustedKey, ...]: The active holders, in listing order.
        """
        return tuple(entry for entry in self.keys if entry.holds(scope) and not self.is_retired(entry))

    @property
    def govern_holders(self) -> tuple[TrustedKey, ...]:
        """The active ``govern`` holders: the keys a revision's quorum can draw on."""
        return self.holders(Scope.GOVERN)

    @property
    def has_governance_margin(self) -> bool:
        """Whether losing one ``govern`` key would still leave a quorum reachable.

        When the quorum equals the number of holders, a single key lost or stolen freezes governance
        permanently: neither the remaining holders nor the attacker can assemble the signatures to
        record a compromise or admit a replacement, while the attacker keeps signing within the
        stolen key's scopes. There is no recovery path inside the protocol -- re-anchoring would be
        exactly the self-assertion the quorum rule exists to forbid -- so the condition is worth
        naming before it is entered rather than diagnosing after (paper Section 8.6).
        """
        return len(self.govern_holders) > self.govern_quorum


class SinceVerdict(StrEnum):
    """What walking the observed revisions says about a key's admission claim.

    Refutation rather than proof, deliberately: a history may be legitimately truncated, so
    presence in an unobserved revision cannot be proven -- but absence from an observed one can.
    """

    CONFIRMED = "confirmed"
    """The admitting revision was observed and the key appears in every observed revision since."""

    REFUTED = "refuted"
    """An observed revision at or after ``since`` does not list the key. The claim is a lie, and
    it is exactly the lie that lets a key that just added itself claim to have been authorized
    from the beginning -- so the whole trust root carrying it is untrustworthy."""

    UNOBSERVABLE = "unobservable"
    """The chain does not reach the admitting revision, and no observed revision refutes the
    claim. Not a failure: a legitimately truncated history looks like this."""


def confirm_since(observed: Sequence[TrustRoot], entry: TrustedKey) -> SinceVerdict:
    """
    Judge a key's ``since`` claim against the revisions actually observed in the chain.

    Args:
        observed (Sequence[TrustRoot]): Every trust-root revision reachable by walking first
            parents, the one in force included. Order does not matter.
        entry (TrustedKey): The entry whose admission claim is being judged.

    Returns:
        SinceVerdict: The verdict. ``REFUTED`` makes the trust root carrying the entry
        untrustworthy; ``UNOBSERVABLE`` is reported, never silently upgraded to confirmed.
    """
    relevant = [root for root in observed if root.revision >= entry.since]
    if any(root.entry_for(entry.key) is None for root in relevant):
        return SinceVerdict.REFUTED
    if any(root.revision == entry.since for root in relevant):
        return SinceVerdict.CONFIRMED
    return SinceVerdict.UNOBSERVABLE
