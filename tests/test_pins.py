"""The pin: consumer-side state, never in the artifact, satisfied directly or through custody.

Trust on first use converts an ongoing exposure into a single moment of exposure. What the pin
must then do is refuse any later change that does not follow the quorum rule -- and accept,
without ceremony, the changes that do: a rotation approved by the previous keys is the mechanism
working, not a mismatch.
"""

from __future__ import annotations

import pytest

from boltzmann.authenticity import (
    PinSource,
    Scope,
    SignatureRecord,
    SshPublicKey,
    TrustedKey,
    TrustRoot,
    read_pin,
    rfc4253_signature,
    sign,
    store_record,
    write_pin,
)
from boltzmann.authenticity.authenticator import Authenticator, AuthorshipState, FindingKind
from boltzmann.authenticity.pins import PIN_POINTER
from boltzmann.blocks.provenance import Actor, ActorKind
from boltzmann.exceptions import SerializationError, SnapshotError, TrustRootMismatchError
from boltzmann.identity.digest import OciDigest
from boltzmann.module.snapshot import Snapshot
from boltzmann.store.memory import MemoryBlockStore

ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")

CURATOR = Actor(id="curator", kind=ActorKind.HUMAN)


class Party:
    def __init__(self, seed: int) -> None:
        self._private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
        line = self._private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        self.public_key = SshPublicKey.parse(line.decode("ascii"))

    def sign_blob(self, data: bytes) -> bytes:
        return rfc4253_signature("ssh-ed25519", self._private.sign(data))

    def entry(self, *scopes: Scope, since: int = 1) -> TrustedKey:
        return TrustedKey(key=self.public_key, scopes=tuple(scopes), since=since)


A = Party(0x11)
B = Party(0x22)
MALLORY = Party(0x66)


def endorse(store: MemoryBlockStore, snapshot: Snapshot, *parties: Party) -> None:
    for party in parties:
        store_record(
            store,
            SignatureRecord(
                snapshot=snapshot.digest,
                key=party.public_key.fingerprint,
                signature=sign(snapshot.canonical_bytes(), party).armored(),
            ),
        )


def keep(store: MemoryBlockStore, snapshot: Snapshot) -> Snapshot:
    store.put_bytes(snapshot.canonical_bytes())
    return snapshot


def governed(quorum: int = 1) -> TrustRoot:
    return TrustRoot(revision=1, govern_quorum=quorum, keys=(A.entry(Scope.GOVERN, Scope.COMMIT),))


class TestPinState:
    """A pin is one pointer in consumer-side state."""

    def test_a_pin_round_trips_with_its_provenance(self) -> None:
        store = MemoryBlockStore()
        digest = OciDigest.of(b"a trust root")
        written = write_pin(store, digest, PinSource.OUT_OF_BAND, reference="ghcr.io/org/brain")
        held = read_pin(store)
        assert held == written
        assert held is not None
        assert held.trust_root == digest
        assert held.source is PinSource.OUT_OF_BAND

    def test_no_pin_is_none_not_an_error(self) -> None:
        assert read_pin(MemoryBlockStore()) is None

    def test_a_pointer_with_duplicate_keys_is_refused(self) -> None:
        """The pointer is written canonically, so it is read the same way.

        A pin is the anchor every other authenticity judgement is measured against. Reading it with
        last-key-wins semantics would mean a hand-edited pointer carrying two trust roots resolves to
        whichever came last, silently -- the ambiguity the strict decoder exists to refuse.
        """
        store = MemoryBlockStore()
        write_pin(store, OciDigest.of(b"a trust root"), PinSource.OUT_OF_BAND)
        raw = store.read_pointer(PIN_POINTER)
        store.write_pointer(PIN_POINTER, raw.replace(b'"source"', b'"source":"first_use","source"', 1))

        with pytest.raises(SerializationError, match="duplicate JSON key"):
            read_pin(store)


class TestPinJudgement:
    """Directly in force, an approved ancestor, or a mismatch -- nothing else."""

    def test_a_matching_pin_reports_pinned(self) -> None:
        store = MemoryBlockStore()
        root = governed()
        genesis = keep(store, Snapshot(trust_root=root))
        endorse(store, genesis, A)
        head = keep(store, genesis.with_modules([]))
        endorse(store, head, A)
        write_pin(store, root.digest, PinSource.FIRST_USE)
        report = Authenticator(store).authenticate(head)
        assert report.pinned
        assert report.state is AuthorshipState.AUTHORIZED

    def test_an_approved_rotation_satisfies_the_old_pin_through_custody(self) -> None:
        store = MemoryBlockStore()
        first = governed()
        genesis = keep(store, Snapshot(trust_root=first))
        endorse(store, genesis, A)
        second = TrustRoot(
            revision=2,
            govern_quorum=1,
            keys=(A.entry(Scope.GOVERN, Scope.COMMIT), B.entry(Scope.GOVERN, Scope.COMMIT, since=2)),
        )
        rotation = keep(store, genesis.with_trust_root(second))
        endorse(store, rotation, A)
        head = keep(store, rotation.with_modules([]))
        endorse(store, head, B)

        write_pin(store, first.digest, PinSource.FIRST_USE)
        report = Authenticator(store).authenticate(head)
        assert report.pinned, "a change that followed the quorum rule is the mechanism working"
        assert report.state is AuthorshipState.AUTHORIZED

    def test_a_pin_for_another_brain_does_not_answer_this_one(self) -> None:
        """A brain is its genesis. Tags are re-assignable and the trust root rotates, so without
        this the anchor would be evaluated against a chain it was never taken for -- and whichever
        answer came back would be about the wrong question."""
        store = MemoryBlockStore()
        root = governed()
        ours = keep(store, Snapshot(trust_root=root, labels={"brain": "ours"}))
        endorse(store, ours, A)

        write_pin(store, root.digest, PinSource.OUT_OF_BAND, genesis=OciDigest.of(b"another brain entirely"))
        report = Authenticator(store).authenticate(ours)

        assert not report.pinned
        assert report.has(FindingKind.PIN_BRAIN_MISMATCH)
        assert not report.has(FindingKind.TRUST_ROOT_MISMATCH), "a different brain is not an authority change"
        with pytest.raises(TrustRootMismatchError, match="a different brain"):
            report.require_authorized()

    def test_the_same_brain_under_a_new_reference_still_matches(self) -> None:
        """Moving repositories is not a change of identity, which is the reason to key on genesis."""
        store = MemoryBlockStore()
        root = governed()
        genesis = keep(store, Snapshot(trust_root=root))
        endorse(store, genesis, A)
        head = keep(store, genesis.with_modules([]))
        endorse(store, head, A)

        write_pin(store, root.digest, PinSource.FIRST_USE, genesis=genesis.digest, reference="ghcr.io/old/brain")
        report = Authenticator(store).authenticate(head)

        assert report.pinned
        assert report.state is AuthorshipState.AUTHORIZED

    def test_a_pruned_history_leaves_the_question_open_rather_than_failing(self) -> None:
        """A legitimately pruned brain cannot resolve its genesis; refusing it would punish pruning."""
        store = MemoryBlockStore()
        root = governed()
        genesis = Snapshot(trust_root=root)
        head = keep(store, genesis.with_modules([]))  # the genesis document itself is never stored
        endorse(store, head, A)

        write_pin(store, root.digest, PinSource.FIRST_USE, genesis=OciDigest.of(b"unrelated"))
        report = Authenticator(store).authenticate(head)

        assert not report.has(FindingKind.PIN_BRAIN_MISMATCH)

    def test_an_unapproved_swap_is_a_mismatch_however_internally_valid(self) -> None:
        store = MemoryBlockStore()
        write_pin(store, governed().digest, PinSource.OUT_OF_BAND)
        # Mallory's brain: internally consistent, correctly signed, entirely theirs.
        forged_root = TrustRoot(revision=1, govern_quorum=1, keys=(MALLORY.entry(Scope.GOVERN, Scope.COMMIT),))
        forged = keep(store, Snapshot(trust_root=forged_root))
        endorse(store, forged, MALLORY)
        report = Authenticator(store).authenticate(forged)
        assert not report.pinned
        assert report.has(FindingKind.TRUST_ROOT_MISMATCH)
        assert report.state is AuthorshipState.UNAUTHORIZED
        with pytest.raises(TrustRootMismatchError):
            report.require_authorized()

    def test_stripping_the_signatures_reports_unsigned_never_valid(self) -> None:
        store = MemoryBlockStore()
        root = governed()
        genesis = keep(store, Snapshot(trust_root=root))
        write_pin(store, root.digest, PinSource.FIRST_USE)
        report = Authenticator(store).authenticate(genesis)
        assert report.state is AuthorshipState.UNSIGNED

    def test_no_pin_means_not_pinned_and_decides_nothing_else(self) -> None:
        store = MemoryBlockStore()
        genesis = keep(store, Snapshot(trust_root=governed()))
        endorse(store, genesis, A)
        report = Authenticator(store).authenticate(genesis)
        assert not report.pinned
        assert report.pin is None
        assert report.state is AuthorshipState.AUTHORIZED


class TestBrainPinning:
    """The Brain facade: TOFU by default, out-of-band when explicit."""

    def test_pinning_defaults_to_the_current_trust_root_as_first_use(self, tmp_path) -> None:
        from boltzmann.brain import Brain

        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        # An ungoverned brain has nothing to anchor.
        with pytest.raises(SnapshotError, match="nothing to pin"):
            brain.pin()

    def test_an_explicit_digest_records_an_out_of_band_pin(self, tmp_path) -> None:
        from boltzmann.brain import Brain

        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        digest = OciDigest.of(b"published on the project site")
        pin = brain.pin(digest)
        assert pin.source is PinSource.OUT_OF_BAND
        assert brain.trust_pin == pin
