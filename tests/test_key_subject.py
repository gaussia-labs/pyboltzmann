"""A signature answers which key signed, and nothing at all about whose it is.

The paper makes the connection load-bearing -- Section 5 says a signature is what turns a declared
actor into an authenticated identity, and Section 8.3 rests the Ed25519 strictness on it -- but no
mechanism existed. A trust-root entry carried five members and none of them was an identity, the
SSH comment is deliberately stripped, and ``Brain.sign`` never reads the actor. So provenance named
a person, the signature named a fingerprint, and nothing asserted they were the same.

These tests pin the new member and, just as importantly, pin what it is *not*: not a certificate,
not a claim any single party can make, and not something an absent value may cost an existing pin.
"""

import re

import pytest

from boltzmann.authenticity.authenticator import Authenticator, AuthorshipState
from boltzmann.authenticity.keys import SshPublicKey, rfc4253_signature
from boltzmann.authenticity.record import SignatureRecord, store_record
from boltzmann.authenticity.scopes import Scope
from boltzmann.authenticity.sshsig import sign
from boltzmann.authenticity.trust_root import TrustedKey, TrustRoot
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import ActorIdError
from boltzmann.identity.digest import BlockId, OciDigest
from boltzmann.module.composition import Composition
from boltzmann.module.snapshot import ModuleRef, Snapshot
from boltzmann.store.memory import MemoryBlockStore

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")


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
JUAN = Party(0xB2)


def root(*, subjects: bool) -> TrustRoot:
    return TrustRoot(
        revision=1,
        govern_quorum=1,
        keys=(
            TrustedKey(
                key=ALEX.public_key,
                subject="alex@example.org" if subjects else None,
                scopes=(Scope.INGEST, Scope.COMMIT, Scope.GOVERN),
                since=1,
            ),
        ),
    )


def signed_snapshot(store: MemoryBlockStore, trust_root: TrustRoot) -> Snapshot:
    composition = Composition(MemoryType.SEMANTIC, [BlockId.of(b"a claim")])
    store.put_bytes(composition.document())
    snapshot = Snapshot(
        trust_root=trust_root,
        modules={
            MemoryType.SEMANTIC: ModuleRef(
                memory_type=MemoryType.SEMANTIC,
                root=composition.root,
                composition=OciDigest.of(composition.document()),
                block_count=len(composition),
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


class TestExistingBrainsAreUntouched:
    def test_a_trust_root_naming_no_subject_keeps_the_digest_it_had(self) -> None:
        """The member is omitted rather than serialized as null, which is what lets every pin taken
        before it existed keep holding. A pin that stopped matching would strand consumers to record
        something they had not asked for."""
        without = root(subjects=False)

        assert b'"subject"' not in without.canonical_bytes()
        assert without.digest == OciDigest.of(without.canonical_bytes())

    def test_naming_a_subject_is_a_different_trust_root(self) -> None:
        """It has to be. A subject inside the signed bytes is what makes it a governed claim rather
        than a note, and changing a governed claim is a revision."""
        assert root(subjects=True).digest != root(subjects=False).digest


class TestWhatASubjectMustBe:
    def test_a_subject_that_is_not_an_actor_identifier_is_refused(self) -> None:
        """A subject exists only to be compared against the actor a provenance record names. One
        that could never match anything is an authoring mistake, caught where it is made."""
        with pytest.raises(ActorIdError, match="subject of key"):
            TrustedKey(key=ALEX.public_key, subject="alex", scopes=(Scope.GOVERN,), since=1)

    def test_the_refusal_names_the_key_it_is_about(self) -> None:
        """A trust root is a list, and a message that did not say which entry was wrong would send
        the author looking through all of them."""
        with pytest.raises(ActorIdError, match=re.escape(ALEX.public_key.fingerprint)):
            TrustedKey(key=ALEX.public_key, subject="Alex@Example.org", scopes=(Scope.GOVERN,), since=1)

    def test_both_identifier_forms_are_accepted(self) -> None:
        """A key held by a service or a pipeline is as ordinary as one held by a person."""
        for subject in ("alex@example.org", "gaussia/release-bot"):
            assert TrustedKey(key=ALEX.public_key, subject=subject, scopes=(Scope.GOVERN,), since=1).subject == subject


class TestWhatAReportSaysNow:
    def test_a_verified_signature_reports_whose_key_it_was(self) -> None:
        store = MemoryBlockStore()
        snapshot = signed_snapshot(store, root(subjects=True))

        authorship = Authenticator(store).authenticate(snapshot).authorship()

        assert authorship.state is AuthorshipState.AUTHORIZED
        assert authorship.key == ALEX.public_key.fingerprint
        assert authorship.subject == "alex@example.org"

    def test_a_brain_that_names_no_subject_reports_none_rather_than_guessing(self) -> None:
        """Silence is the honest answer. Inferring a holder from anything else -- an agent comment,
        a provenance record, the only actor in the brain -- would be manufacturing the very claim
        the trust root exists to make deliberately."""
        store = MemoryBlockStore()
        snapshot = signed_snapshot(store, root(subjects=False))

        authorship = Authenticator(store).authenticate(snapshot).authorship()

        assert authorship.state is AuthorshipState.AUTHORIZED
        assert authorship.key == ALEX.public_key.fingerprint
        assert authorship.subject is None

    def test_an_attributable_key_has_no_subject_to_report(self) -> None:
        """By definition it is not in the trust root, so nobody in this brain has said whose it is
        -- and that absence is the entire state, not a gap in the report."""
        store = MemoryBlockStore()
        composition = Composition(MemoryType.SEMANTIC, [BlockId.of(b"a stranger's claim")])
        store.put_bytes(composition.document())
        snapshot = Snapshot(
            trust_root=root(subjects=True),
            modules={
                MemoryType.SEMANTIC: ModuleRef(
                    memory_type=MemoryType.SEMANTIC,
                    root=composition.root,
                    composition=OciDigest.of(composition.document()),
                    block_count=len(composition),
                )
            },
        )
        store.put_bytes(snapshot.canonical_bytes())
        store_record(
            store,
            SignatureRecord(
                snapshot=snapshot.digest,
                key=JUAN.public_key.fingerprint,
                signature=sign(snapshot.canonical_bytes(), JUAN).armored(),
            ),
        )

        from boltzmann.authenticity.authenticator import SnapshotStance

        authorship = Authenticator(store).authenticate(snapshot, stance=SnapshotStance.OFFERED).authorship()

        assert authorship.state is AuthorshipState.ATTRIBUTABLE
        assert authorship.key == JUAN.public_key.fingerprint
        assert authorship.subject is None

    def test_each_signature_verdict_carries_the_subject_too(self) -> None:
        """A quorum is several signatures, and a report that named only one holder would be
        unreadable exactly where it matters most."""
        store = MemoryBlockStore()
        snapshot = signed_snapshot(store, root(subjects=True))

        report = Authenticator(store).authenticate(snapshot)

        assert [verdict.subject for verdict in report.signatures] == ["alex@example.org"]
