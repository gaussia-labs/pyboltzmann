"""Governance through the Brain facade: authority starts at init and moves only by quorum.

The multi-party flow is the one worth defending end to end: the revision document is built once,
its exact bytes travel, each party inspects before signing, and the head moves only when the
quorum -- evaluated against the key list being *replaced* -- is satisfied. A failed quorum
advances nothing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from boltzmann.authenticity import (
    Scope,
    SshPublicKey,
    TrustedKey,
    TrustRoot,
    rfc4253_signature,
)
from boltzmann.authenticity.authenticator import AuthorshipState, FindingKind, SignatureOutcome
from boltzmann.blocks.provenance import Actor, ActorKind
from boltzmann.brain import Brain
from boltzmann.exceptions import (
    QuorumFailureError,
    ResolutionRefusedError,
    SnapshotError,
)
from boltzmann.identity.digest import OciDigest

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


A = Party(0x51)
B = Party(0x52)
C = Party(0x53)


def sole_owner() -> TrustRoot:
    return TrustRoot(
        revision=1,
        govern_quorum=1,
        keys=(A.entry(Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.REDACT, Scope.GOVERN),),
    )


def two_owners() -> TrustRoot:
    return TrustRoot(
        revision=1,
        govern_quorum=2,
        keys=(A.entry(Scope.COMMIT, Scope.GOVERN), B.entry(Scope.COMMIT, Scope.GOVERN)),
    )


class TestInit:
    """The founding act: asserted, signed, and never repeated."""

    def test_init_produces_an_authorized_governed_genesis(self, tmp_path: Path) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner(), signers=[A])
        assert brain.trust_root == sole_owner()
        report = brain.authenticate()
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.role.value == "genesis"

    def test_a_directory_holds_exactly_one_genesis(self, tmp_path: Path) -> None:
        Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner(), signers=[A])
        with pytest.raises(SnapshotError, match="exactly one genesis"):
            Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner())

    def test_a_genesis_below_its_declared_quorum_warns_and_proceeds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=two_owners(), signers=[A])
        assert any("departs from the rule" in message for message in caplog.messages)

    def test_an_unsigned_genesis_is_permitted_and_reported_unsigned(self, tmp_path: Path) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner())
        assert brain.authenticate().state is AuthorshipState.UNSIGNED


class TestGovernanceMargin:
    """A quorum with no slack is legal, and one lost key ends governance for good."""

    def with_margin(self) -> TrustRoot:
        return TrustRoot(
            revision=1,
            govern_quorum=2,
            keys=(
                A.entry(Scope.COMMIT, Scope.GOVERN),
                B.entry(Scope.COMMIT, Scope.GOVERN),
                C.entry(Scope.COMMIT, Scope.GOVERN),
            ),
        )

    def test_a_quorum_equal_to_its_holders_has_no_margin(self) -> None:
        assert not two_owners().has_governance_margin
        assert not sole_owner().has_governance_margin
        assert self.with_margin().has_governance_margin

    def test_init_warns_when_the_margin_is_gone(self, tmp_path: Path, caplog) -> None:
        """At the moment the margin is chosen: afterwards there is nothing to be done about it."""
        with caplog.at_level("WARNING"):
            Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=two_owners(), signers=[A, B])
        assert "freezes governance permanently" in caplog.text

    def test_init_is_quiet_when_a_key_could_be_lost(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level("WARNING"):
            Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=self.with_margin(), signers=[A, B])
        assert "freezes governance permanently" not in caplog.text

    def test_a_revision_that_removes_the_margin_warns(self, tmp_path: Path, caplog) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=self.with_margin(), signers=[A, B])
        tightened = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(A.entry(Scope.COMMIT, Scope.GOVERN), B.entry(Scope.COMMIT, Scope.GOVERN)),
        )
        with caplog.at_level("WARNING"):
            brain.rotate(tightened, signers=[A, B])
        assert "freezes governance permanently" in caplog.text

    def test_the_report_names_it_without_blocking(self, tmp_path: Path) -> None:
        """A verifier meeting the brain later should see the condition; it is not a failure."""
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=two_owners(), signers=[A, B])
        report = brain.authenticate()

        assert report.has(FindingKind.QUORUM_MARGIN)
        assert report.state is AuthorshipState.AUTHORIZED
        assert not any(f.blocking for f in report.findings if f.kind is FindingKind.QUORUM_MARGIN)


class TestSign:
    """A signature lands beside the snapshot and claims what the snapshot did."""

    def test_the_claimed_scopes_default_to_the_computed_requirement(self, tmp_path: Path) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner())
        record = brain.sign(A)
        assert record.scopes == (Scope.GOVERN,), "a governed genesis requires exactly govern"
        assert brain.signatures() == [record]

    def test_signing_changes_no_identity(self, tmp_path: Path) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner())
        before = brain.snapshot().digest
        brain.sign(A)
        brain.sign(A)  # deterministic Ed25519: an honest re-sign is the same record
        assert brain.snapshot().digest == before
        assert len(brain.signatures()) == 1


class TestSingleOwnerRotation:
    """One key, one call: the paper's single-author deployment."""

    def test_admitting_a_second_key_is_one_rotate_call(self, tmp_path: Path) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner(), signers=[A])
        revised = TrustRoot(
            revision=2,
            govern_quorum=1,
            keys=(*sole_owner().keys, B.entry(Scope.COMMIT, since=2)),
        )
        result = brain.rotate(revised, signers=[A])
        assert result.revision == 2
        assert result.quorum_met == 1
        assert brain.trust_root == revised
        report = brain.authenticate()
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.role.value == "revision"

    def test_an_ungoverned_brain_cannot_rotate(self, tmp_path: Path) -> None:
        brain = Brain.open(tmp_path / "brain", actor=CURATOR)
        with pytest.raises(SnapshotError, match="anchored at a genesis"):
            brain.rotate(sole_owner(), signers=[A])

    def test_exactly_one_of_trust_root_or_plan(self, tmp_path: Path) -> None:
        brain = Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=sole_owner(), signers=[A])
        with pytest.raises(SnapshotError, match="exactly one"):
            brain.rotate()


class TestMultiPartyRotation:
    """Two owners, two machines: the document travels, the signatures come back."""

    def build_pair(self, tmp_path: Path) -> tuple[Brain, Brain]:
        origin = tmp_path / "brain-a"
        brain_a = Brain.init(origin, actor=CURATOR, trust_root=two_owners(), signers=[A, B])
        shutil.copytree(origin, tmp_path / "brain-b")
        brain_b = Brain.open(tmp_path / "brain-b", actor=CURATOR)
        return brain_a, brain_b

    def admitting_c(self) -> TrustRoot:
        return TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(*two_owners().keys, C.entry(Scope.COMMIT, since=2)),
        )

    def test_the_full_flow_document_out_records_back_head_advances(self, tmp_path: Path) -> None:
        brain_a, brain_b = self.build_pair(tmp_path)
        plan = brain_a.plan_rotate(self.admitting_c())
        assert plan.quorum_required == 2
        assert set(plan.eligible) == {A.public_key.fingerprint, B.public_key.fingerprint}

        # Each side inspects and signs the exact bytes; the ~300-byte records travel back.
        record_a = brain_a.countersign(plan.document, A)
        record_b = brain_b.countersign(plan.document, B)

        result = brain_a.rotate(plan=plan, records=[record_a, record_b])
        assert result.quorum_met == 2
        assert brain_a.trust_root == self.admitting_c()
        assert brain_a.authenticate().state is AuthorshipState.AUTHORIZED

    def test_an_incomplete_quorum_advances_nothing(self, tmp_path: Path) -> None:
        brain_a, _ = self.build_pair(tmp_path)
        head = brain_a.snapshot().digest
        plan = brain_a.plan_rotate(self.admitting_c())
        record_a = brain_a.countersign(plan.document, A)
        with pytest.raises(QuorumFailureError, match="1 qualified"):
            brain_a.rotate(plan=plan, records=[record_a])
        assert brain_a.snapshot().digest == head, "a failed quorum advances nothing"

    def test_a_record_over_a_rebuilt_document_is_caught_with_directions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        brain_a, brain_b = self.build_pair(tmp_path)
        plan = brain_a.plan_rotate(self.admitting_c())
        record_b = brain_b.countersign(plan.document, B)
        # Rebuilding instead of passing plan= mints a different created_at and digest. Within
        # one second the rebuild would be byte-identical -- which is fine -- so the clock is
        # moved to make the mistake visible, as it would be in any real exchange.
        monkeypatch.setattr("boltzmann.module.snapshot.utc_timestamp", lambda: "2027-01-01T00:00:00Z")
        with pytest.raises(SnapshotError, match=r"pass\s+plan="):
            brain_a.rotate(self.admitting_c(), signers=[A], records=[record_b])

    def test_countersign_refuses_a_parent_it_cannot_see(self, tmp_path: Path) -> None:
        brain_a, _ = self.build_pair(tmp_path)
        stranger = Brain.init(tmp_path / "stranger", actor=CURATOR, trust_root=sole_owner(), signers=[A])
        plan = brain_a.plan_rotate(self.admitting_c())
        with pytest.raises(ResolutionRefusedError, match="not held here"):
            stranger.countersign(plan.document, A)

    def test_countersign_refuses_content_smuggled_into_governance(self, tmp_path: Path) -> None:
        from boltzmann.blocks.memory_type import MemoryType
        from boltzmann.identity.digest import BlockId
        from boltzmann.module.composition import Composition
        from boltzmann.module.snapshot import ModuleRef, Snapshot

        brain_a, brain_b = self.build_pair(tmp_path)
        composition = Composition(MemoryType.SEMANTIC, [BlockId.of(b"smuggled")])
        impure = Snapshot(
            modules={
                MemoryType.SEMANTIC: ModuleRef(
                    memory_type=MemoryType.SEMANTIC,
                    root=composition.root,
                    composition=OciDigest.of(composition.document()),
                    block_count=1,
                )
            },
            parents=[brain_a.snapshot().digest],
            trust_root=self.admitting_c(),
        )
        with pytest.raises(ResolutionRefusedError, match="smuggled"):
            brain_b.countersign(impure.canonical_bytes(), B)

    def test_countersign_refuses_non_canonical_bytes(self, tmp_path: Path) -> None:
        brain_a, brain_b = self.build_pair(tmp_path)
        plan = brain_a.plan_rotate(self.admitting_c())
        prettified = plan.document.replace(b",", b", ")
        with pytest.raises(SnapshotError, match="canonical"):
            brain_b.countersign(prettified, B)

    def test_a_moved_head_invalidates_the_plan_with_directions(self, tmp_path: Path) -> None:
        brain_a, _ = self.build_pair(tmp_path)
        plan = brain_a.plan_rotate(self.admitting_c())
        record_a = brain_a.countersign(plan.document, A)
        record_b = brain_a.countersign(plan.document, B)
        moved = TrustRoot(revision=2, govern_quorum=2, keys=two_owners().keys)
        brain_a.rotate(moved, signers=[A, B])
        with pytest.raises(SnapshotError, match="plan again"):
            brain_a.rotate(plan=plan, records=[record_a, record_b])


class TestRevocation:
    """Retirement and compromise: similar calls, opposite behaviours."""

    def governed(self, tmp_path: Path) -> Brain:
        root = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(A.entry(Scope.COMMIT, Scope.GOVERN), B.entry(Scope.COMMIT)),
        )
        return Brain.init(tmp_path / "brain", actor=CURATOR, trust_root=root, signers=[A])

    def test_a_retirement_defaults_to_the_revision_it_creates(self, tmp_path: Path) -> None:
        brain = self.governed(tmp_path)
        result = brain.revoke(B.public_key, signers=[A])
        assert result.revision == 2
        entry = brain.trust_root.entry_for(B.public_key)  # type: ignore[union-attr]
        assert entry is not None
        assert entry.retired_from == 2
        assert entry.compromised_from is None

    def test_a_compromise_records_the_snapshot_position(self, tmp_path: Path) -> None:
        brain = self.governed(tmp_path)
        position = brain.snapshot().digest
        brain.revoke(B.public_key.fingerprint, signers=[A], compromised_from=position)
        entry = brain.trust_root.entry_for(B.public_key)  # type: ignore[union-attr]
        assert entry is not None
        assert entry.compromised_from == position
        assert entry.retired_from is None

    def test_a_retired_keys_later_signature_fails_while_the_brain_stays_authorized(self, tmp_path: Path) -> None:
        brain = self.governed(tmp_path)
        brain.revoke(B.public_key, signers=[A])
        # On the revision snapshot itself the trust root in force is still revision 1, where B
        # is not yet retired -- retirement is judged at the parent's position. B's failure there
        # is an insufficient scope (no govern), not a retirement:
        brain.sign(B)
        assert brain.authenticate().outcomes()[B.public_key.fingerprint] is SignatureOutcome.INSUFFICIENT_SCOPE
        # One snapshot later, revision 2 is in force and the retirement has taken effect.
        third = TrustRoot(
            revision=3,
            govern_quorum=1,
            keys=(*brain.trust_root.keys, C.entry(Scope.COMMIT, since=3)),  # type: ignore[union-attr]
        )
        brain.rotate(third, signers=[A])
        brain.sign(B)
        report = brain.authenticate()
        assert report.outcomes()[B.public_key.fingerprint] is SignatureOutcome.RETIRED_KEY
        assert report.state is AuthorshipState.AUTHORIZED, "A's signature carries the head regardless"

    def test_both_positions_at_once_mean_nothing(self, tmp_path: Path) -> None:
        brain = self.governed(tmp_path)
        with pytest.raises(ValueError, match="opposite intents"):
            brain.revoke(B.public_key, signers=[A], retired_from=2, compromised_from=OciDigest.of(b"x"))

    def test_an_unlisted_key_has_nothing_to_revoke(self, tmp_path: Path) -> None:
        brain = self.governed(tmp_path)
        with pytest.raises(SnapshotError, match="nothing to revoke"):
            brain.revoke(C.public_key, signers=[A])

    def test_a_second_retirement_records_nothing(self, tmp_path: Path) -> None:
        brain = self.governed(tmp_path)
        brain.revoke(B.public_key, signers=[A])
        with pytest.raises(SnapshotError, match="already retired"):
            brain.revoke(B.public_key, signers=[A])
