"""Which of a snapshot's claimed actors its signatures actually stand behind.

Until a key stands behind a name, the actor in a provenance record is a *declared* identifier:
whoever can write to a brain can write any name into its audit trail. The trust root's ``subject``
makes the claim checkable, and this is where the two are finally put side by side.

The hard part is not the comparison, it is the restraint. A snapshot legitimately names actors that
never signed it -- every merge does -- so a verifier that refused an unvouched actor would refuse
reconciliation itself. These tests pin both halves: that the comparison is made, and that it never
decides anything.
"""

import re

import pytest

from boltzmann.authenticity.attribution import check_attribution
from boltzmann.authenticity.authenticator import Authenticator, AuthorshipState, FindingKind
from boltzmann.authenticity.keys import SshPublicKey, rfc4253_signature
from boltzmann.authenticity.record import SignatureRecord, store_record
from boltzmann.authenticity.scopes import Scope
from boltzmann.authenticity.sshsig import sign
from boltzmann.authenticity.trust_root import TrustedKey, TrustRoot
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.blocks.provenance import (
    Actor,
    ActorKind,
    Collaborator,
    RegistrationRecord,
    provenance_block,
)
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.store.memory import MemoryBlockStore

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")

AT = "2026-08-31T00:00:00Z"
ALEX_ID = "alex@example.org"
JUAN_ID = "juan@example.org"


class Party:
    """One key holder. The seeds are published test values; never real keys."""

    def __init__(self, seed: int) -> None:
        self._private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        line = self._private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        self.public_key = SshPublicKey.parse(line.decode("ascii"))

    def sign_blob(self, data: bytes) -> bytes:
        return rfc4253_signature("ssh-ed25519", self._private.sign(data))


ALEX = Party(0xA1)


def brain_signed_by_alex(
    store: MemoryBlockStore,
    *,
    actors: list[Actor],
    subject: str | None = ALEX_ID,
    assisted_by: list[Collaborator] | None = None,
) -> Snapshot:
    """A snapshot whose provenance names ``actors``, signed by Alex's key."""
    records = [
        provenance_block(
            RegistrationRecord(block=BlockId.of(actor.id.encode()), actor=actor, at=AT),
            assisted_by,
        )
        for actor in actors
    ]
    for record in records:
        store.put_block(record)

    provenance = Composition(MemoryType.PROVENANCE, [record.block_id for record in records])
    store.put_bytes(provenance.document())
    snapshot = Snapshot(
        trust_root=TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(
                TrustedKey(
                    key=ALEX.public_key,
                    subject=subject,
                    scopes=(Scope.INGEST, Scope.COMMIT, Scope.GOVERN),
                    since=1,
                ),
            ),
        ),
        modules={
            MemoryType.PROVENANCE: ModuleRef(
                memory_type=MemoryType.PROVENANCE,
                root=provenance.root,
                composition=OciDigest.of(provenance.document()),
                block_count=len(provenance),
            )
        },
    )
    store.put_bytes(snapshot.canonical_bytes())
    store_record(
        store,
        SignatureRecord(
            snapshot=snapshot.digest,
            key=ALEX.public_key.fingerprint,
            signature=sign(snapshot.canonical_bytes(), ALEX).armored(),
        ),
    )
    return snapshot


def person(identifier: str) -> Actor:
    return Actor(id=identifier, kind=ActorKind.HUMAN)


class TestWhatASignatureVouchesFor:
    def test_an_actor_matching_the_signing_key_reports_verified(self) -> None:
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(ALEX_ID)])

        attribution = Authenticator(store).authenticate(snapshot).attribution

        assert attribution.verified == (ALEX_ID,)
        assert attribution.asserted == ()
        assert attribution.is_fully_vouched

    def test_an_actor_nobody_signed_for_reports_asserted(self) -> None:
        """Not an accusation. It is the ordinary state of a contribution that arrived by merge, and
        of every brain whose trust root names no subjects at all."""
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(ALEX_ID), person(JUAN_ID)])

        attribution = Authenticator(store).authenticate(snapshot).attribution

        assert attribution.verified == (ALEX_ID,)
        assert attribution.asserted == (JUAN_ID,)

    def test_a_brain_naming_no_subjects_vouches_for_nothing_and_says_so(self) -> None:
        """The state every brain was in before subjects existed, reported rather than assumed away."""
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(ALEX_ID)], subject=None)

        attribution = Authenticator(store).authenticate(snapshot).attribution

        assert attribution.verified == ()
        assert attribution.asserted == (ALEX_ID,)

    def test_a_legacy_identifier_is_reported_apart_from_an_unvouched_one(self) -> None:
        """The remedy differs, which is the whole reason to separate them: an unvouched actor needs a
        governance act, and a legacy one needs a rewrite nobody can perform on published bytes."""
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(ALEX_ID), person("curator")])

        attribution = Authenticator(store).authenticate(snapshot).attribution

        assert attribution.verified == (ALEX_ID,)
        assert attribution.asserted == ()
        assert attribution.legacy == ("curator",)

    def test_an_assisting_party_is_never_expected_to_have_signed(self) -> None:
        """Nothing expects a model to hold a key. Counting its absence would bury the one comparison
        that means something under one that never could."""
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(
            store,
            actors=[person(ALEX_ID)],
            assisted_by=[Collaborator(id="anthropic/claude-code", kind=ActorKind.AGENT, model="anthropic/fable-5")],
        )

        attribution = Authenticator(store).authenticate(snapshot).attribution

        assert attribution.is_fully_vouched
        assert "anthropic" not in attribution.detail


class TestItNeverDecidesAnything:
    def test_an_unvouched_actor_does_not_keep_a_snapshot_from_being_authorized(self) -> None:
        """The restraint is the point. Refusing here would refuse every merge, because
        reconciliation is exactly the operation that brings in records their authors did not sign."""
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(ALEX_ID), person(JUAN_ID)])

        report = Authenticator(store).authenticate(snapshot)

        assert report.state is AuthorshipState.AUTHORIZED
        assert report.has(FindingKind.ATTRIBUTION_UNVERIFIED)
        report.require_authorized()

    def test_the_finding_is_not_blocking(self) -> None:
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(JUAN_ID)])

        report = Authenticator(store).authenticate(snapshot)

        assert not any(f.blocking for f in report.findings if f.kind is FindingKind.ATTRIBUTION_UNVERIFIED)

    def test_a_fully_vouched_snapshot_raises_no_finding_at_all(self) -> None:
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(ALEX_ID)])

        assert not Authenticator(store).authenticate(snapshot).has(FindingKind.ATTRIBUTION_UNVERIFIED)

    def test_the_finding_names_who_is_unvouched(self) -> None:
        """A finding a reader cannot act on is one they will learn to ignore."""
        store = MemoryBlockStore()
        snapshot = brain_signed_by_alex(store, actors=[person(JUAN_ID)])

        report = Authenticator(store).authenticate(snapshot)

        assert re.search(JUAN_ID, report.detail(FindingKind.ATTRIBUTION_UNVERIFIED))


class TestWhatCannotBeAsked:
    def test_a_snapshot_with_no_provenance_module_is_simply_empty(self) -> None:
        store = MemoryBlockStore()
        snapshot = Snapshot()

        report = check_attribution(store, snapshot, [ALEX_ID])

        assert report.is_complete
        assert report.is_fully_vouched

    def test_an_unresolvable_composition_is_reported_rather_than_passed(self) -> None:
        """Silence would let a withheld composition turn the comparison off."""
        store = MemoryBlockStore()
        snapshot = Snapshot(
            modules={
                MemoryType.PROVENANCE: ModuleRef(
                    memory_type=MemoryType.PROVENANCE,
                    root=Composition(MemoryType.PROVENANCE).root,
                    composition=OciDigest.of(b"a composition nobody holds"),
                    block_count=0,
                )
            }
        )

        report = check_attribution(store, snapshot, [ALEX_ID])

        assert not report.is_complete
        assert "not resolvable" in report.detail
