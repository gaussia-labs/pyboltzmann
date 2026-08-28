"""The paper's three worked cases, end to end (Section 8.9), plus retirement and compromise.

The acceptance criterion for the whole subsystem. Case 1: an owner admitted by quorum signs
ordinarily afterwards. Case 2: the same key list, byte-identical, self-admitted -- rejected with
exactly `unauthorized_key` and `quorum_failure`, and **with no pin at all**, because the verifier
is checking an internal transition rule, not comparing against an external anchor. Case 3: a
genesis is not validated, it is anchored.
"""

from __future__ import annotations

import pytest

from boltzmann.authenticity import (
    Scope,
    SignatureRecord,
    SshPublicKey,
    TrustedKey,
    TrustRoot,
    rfc4253_signature,
    sign,
    store_record,
)
from boltzmann.authenticity.authenticator import (
    Authenticator,
    AuthorshipState,
    FindingKind,
    SignatureOutcome,
)
from boltzmann.authenticity.policy import VerificationPolicy
from boltzmann.blocks.memory_type import MemoryType
from boltzmann.exceptions import (
    CompromisedKeyError,
    QuorumFailureError,
    RetiredKeyError,
    UnauthorizedKeyError,
    UnsignedBrainError,
)
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

    def entry(self, *scopes: Scope, since: int = 1, **positions: object) -> TrustedKey:
        return TrustedKey(key=self.public_key, scopes=tuple(scopes), since=since, **positions)  # type: ignore[arg-type]


A = Party(0xA1)
B = Party(0xB2)
C = Party(0xC3)


def endorse(store: MemoryBlockStore, snapshot: Snapshot, *parties: Party) -> None:
    """Sign a snapshot and keep the records, the way rotate and sign will."""
    for party in parties:
        record = SignatureRecord(
            snapshot=snapshot.digest,
            key=party.public_key.fingerprint,
            signature=sign(snapshot.canonical_bytes(), party).armored(),
        )
        store_record(store, record)


def keep(store: MemoryBlockStore, snapshot: Snapshot) -> Snapshot:
    store.put_bytes(snapshot.canonical_bytes())
    return snapshot


def advance(store: MemoryBlockStore, snapshot: Snapshot, *labels: str) -> Snapshot:
    """One ordinary commit: the semantic module gains blocks."""
    composition = Composition(MemoryType.SEMANTIC, [BlockId.of(label.encode()) for label in labels])
    store.put_bytes(composition.document())
    reference = ModuleRef(
        memory_type=MemoryType.SEMANTIC,
        root=composition.root,
        composition=OciDigest.of(composition.document()),
        block_count=len(composition),
    )
    return keep(store, snapshot.with_modules([reference]))


@pytest.fixture
def store() -> MemoryBlockStore:
    return MemoryBlockStore()


def revision_one() -> TrustRoot:
    return TrustRoot(
        revision=1,
        govern_quorum=2,
        keys=(
            A.entry(Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.GOVERN),
            B.entry(Scope.INGEST, Scope.COMMIT, Scope.GOVERN),
        ),
    )


@pytest.fixture
def chain(store: MemoryBlockStore) -> Snapshot:
    """A governed brain at S7: genesis under revision 1, then one commit by A."""
    genesis = keep(store, Snapshot(trust_root=revision_one()))
    endorse(store, genesis, A, B)
    seven = advance(store, genesis, "concept one")
    endorse(store, seven, A)
    return seven


class TestCaseOneAdmittingAThirdOwner:
    """C sends the public half; A and B admit it by quorum; C then commits ordinarily."""

    def admit(self, store: MemoryBlockStore, seven: Snapshot) -> Snapshot:
        revised = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(*revision_one().keys, C.entry(Scope.COMMIT, since=2)),
        )
        eight = keep(store, seven.with_trust_root(revised))
        endorse(store, eight, A, B)
        return eight

    def test_the_revision_is_authorized_under_the_previous_key_list(
        self, store: MemoryBlockStore, chain: Snapshot
    ) -> None:
        eight = self.admit(store, chain)
        report = Authenticator(store).authenticate(eight)
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.role.value == "revision"
        assert report.quorum_required == 2
        assert report.quorum_met == 2
        assert report.required_scopes == (Scope.GOVERN,)

    def test_the_admitted_keys_later_commit_verifies(self, store: MemoryBlockStore, chain: Snapshot) -> None:
        eight = self.admit(store, chain)
        nine = advance(store, eight, "concept one", "concept two")
        endorse(store, nine, C)
        report = Authenticator(store).authenticate(nine)
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.trust_root_revision == 2
        assert report.required_scopes == (Scope.COMMIT,)
        assert report.outcomes()[C.public_key.fingerprint] is SignatureOutcome.VALID

    def test_the_new_key_cannot_do_what_it_was_not_granted(self, store: MemoryBlockStore, chain: Snapshot) -> None:
        eight = self.admit(store, chain)
        revised = TrustRoot(revision=3, govern_quorum=2, keys=eight.trust_root.keys)  # type: ignore[union-attr]
        ten = keep(store, eight.with_trust_root(revised))
        endorse(store, ten, C)
        report = Authenticator(store).authenticate(ten)
        assert report.state is AuthorshipState.UNAUTHORIZED
        assert report.outcomes()[C.public_key.fingerprint] is SignatureOutcome.INSUFFICIENT_SCOPE
        assert report.has(FindingKind.QUORUM_FAILURE)


class TestCaseTwoAKeyAdmittingItself:
    """The key list may be byte-identical to Case 1's. What differs is who signed the snapshot
    that introduced it -- and the rejection needs no pin, because the verifier is checking an
    internal transition rule."""

    def test_self_admission_fails_with_exactly_the_two_predicted_findings(
        self, store: MemoryBlockStore, chain: Snapshot
    ) -> None:
        forged = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(*revision_one().keys, C.entry(Scope.COMMIT, Scope.GOVERN, since=2)),
        )
        eight_prime = keep(store, chain.with_trust_root(forged))
        endorse(store, eight_prime, C)

        report = Authenticator(store).authenticate(eight_prime)
        assert report.state is AuthorshipState.UNAUTHORIZED
        assert report.pin is None, "the rejection must not depend on any pin"
        # The first two checks genuinely pass -- the structure recomputes and the signature
        # verifies -- which is the whole reason authenticity exists. What fails:
        assert report.outcomes()[C.public_key.fingerprint] is SignatureOutcome.UNAUTHORIZED_KEY
        assert report.has(FindingKind.QUORUM_FAILURE)
        assert report.quorum_met == 0
        with pytest.raises(QuorumFailureError):
            report.require_authorized()

    def test_rejection_propagates_to_the_forged_subtree(self, store: MemoryBlockStore, chain: Snapshot) -> None:
        forged = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(*revision_one().keys, C.entry(Scope.COMMIT, since=2)),
        )
        eight_prime = keep(store, chain.with_trust_root(forged))
        endorse(store, eight_prime, C)
        nine_prime = advance(store, eight_prime, "concept one", "poison")
        endorse(store, nine_prime, C)
        # The descendant draws its trust root from a snapshot already rejected; the since check
        # refutes C's claim against the observable chain.
        report = Authenticator(store).authenticate(nine_prime)
        assert report.state is AuthorshipState.UNAUTHORIZED


class TestCaseThreeStartingFromNothing:
    """A genesis is not validated, it is anchored."""

    def test_a_genesis_satisfying_its_own_quorum_is_authorized_and_exempt(self, store: MemoryBlockStore) -> None:
        genesis = keep(store, Snapshot(trust_root=revision_one()))
        endorse(store, genesis, A, B)
        report = Authenticator(store).authenticate(genesis)
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.role.value == "genesis"
        assert not report.has(FindingKind.QUORUM_FAILURE), "the quorum rule does not govern a genesis"

    def test_a_genesis_below_its_own_declared_quorum_warns_without_blocking(self, store: MemoryBlockStore) -> None:
        genesis = keep(store, Snapshot(trust_root=revision_one()))
        endorse(store, genesis, A)
        report = Authenticator(store).authenticate(genesis)
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.has(FindingKind.GENESIS_BELOW_QUORUM)
        assert all(f.blocking is False for f in report.findings if f.kind is FindingKind.GENESIS_BELOW_QUORUM)

    def test_an_unsigned_genesis_is_unsigned_not_invalid(self, store: MemoryBlockStore) -> None:
        genesis = keep(store, Snapshot(trust_root=revision_one()))
        report = Authenticator(store).authenticate(genesis)
        assert report.state is AuthorshipState.UNSIGNED
        with pytest.raises(UnsignedBrainError):
            report.require_authorized()


class TestRetirement:
    """Retirement closes an interval without disturbing what came before it."""

    def retire_b(self, store: MemoryBlockStore, seven: Snapshot) -> Snapshot:
        revised = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(
                A.entry(Scope.INGEST, Scope.COMMIT, Scope.DROP_CANONICAL, Scope.GOVERN),
                B.entry(Scope.INGEST, Scope.COMMIT, Scope.GOVERN, retired_from=2),
            ),
        )
        eight = keep(store, seven.with_trust_root(revised))
        endorse(store, eight, A, B)  # B still held govern in revision 1, so B may co-sign its own departure
        return eight

    def test_a_retired_key_counted_toward_the_quorum_that_retired_it(
        self, store: MemoryBlockStore, chain: Snapshot
    ) -> None:
        # The half that is easy to lose: retirement is judged at the PARENT's revision.
        eight = self.retire_b(store, chain)
        report = Authenticator(store).authenticate(eight)
        assert report.state is AuthorshipState.AUTHORIZED
        assert report.quorum_met == 2

    def test_signatures_before_the_retirement_stand_and_later_ones_fail(
        self, store: MemoryBlockStore, chain: Snapshot
    ) -> None:
        eight = self.retire_b(store, chain)
        nine = advance(store, eight, "concept one", "concept two")
        endorse(store, nine, B)
        late = Authenticator(store).authenticate(nine)
        assert late.state is AuthorshipState.UNAUTHORIZED
        assert late.outcomes()[B.public_key.fingerprint] is SignatureOutcome.RETIRED_KEY
        with pytest.raises(RetiredKeyError):
            late.require_authorized()

        # The commit B made before departing is untouched -- an ordinary departure is harmless.
        early_seven = Authenticator(store).authenticate(chain)
        assert early_seven.state is AuthorshipState.AUTHORIZED


class TestCompromise:
    """The only construct that withdraws a previously valid signature, reported as such."""

    def build(self, store: MemoryBlockStore) -> tuple[Snapshot, Snapshot, Snapshot]:
        """Genesis(r1: A govern, C commit) -> S1 (C) -> S2 (C) -> revision r2: C compromised from S2."""
        first = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(A.entry(Scope.GOVERN, Scope.COMMIT), C.entry(Scope.COMMIT)),
        )
        genesis = keep(store, Snapshot(trust_root=first))
        endorse(store, genesis, A)
        one = advance(store, genesis, "honest work")
        endorse(store, one, C)
        two = advance(store, one, "honest work", "stolen key era")
        endorse(store, two, C)
        recorded = TrustRoot(
            revision=2,
            govern_quorum=1,
            keys=(
                A.entry(Scope.GOVERN, Scope.COMMIT),
                C.entry(Scope.COMMIT, compromised_from=two.digest),
            ),
        )
        head = keep(store, two.with_trust_root(recorded))
        endorse(store, head, A)
        return one, two, head

    def test_signatures_from_the_compromise_position_onward_are_withdrawn(self, store: MemoryBlockStore) -> None:
        _, two, head = self.build(store)
        report = Authenticator(store).authenticate(two, current=head.trust_root)
        assert report.state is AuthorshipState.UNAUTHORIZED
        assert [verdict.key for verdict in report.withdrawn] == [C.public_key.fingerprint]
        assert report.has(FindingKind.COMPROMISED_KEY)
        with pytest.raises(CompromisedKeyError):
            report.require_authorized()

    def test_signatures_before_the_compromise_position_stand(self, store: MemoryBlockStore) -> None:
        one, _, head = self.build(store)
        report = Authenticator(store).authenticate(one, current=head.trust_root)
        assert report.state is AuthorshipState.AUTHORIZED
        assert not report.withdrawn

    def test_a_backdated_timestamp_decides_nothing(self, store: MemoryBlockStore) -> None:
        # Validity is positional: whatever created_at claims, the chain position rules.
        _, two, head = self.build(store)
        assert Authenticator(store).authenticate(two, current=head.trust_root).state is (AuthorshipState.UNAUTHORIZED)


class TestCompromiseAcrossMerges:
    """Revocation reachability walks every parent: a merged-in compromise still happened.

    Authorization stays first-parent-only, but ``descends_from`` may not look down one branch of
    a reconciliation and declare a key cleared because the compromise sat on the other.
    """

    def build(self, store: MemoryBlockStore) -> tuple[Snapshot, Snapshot, Snapshot]:
        """Genesis -> main; genesis -> side (the stolen-key era); merge(main, side); r2 records
        the compromise at the side position, reachable only through the second parent."""
        first = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(A.entry(Scope.GOVERN, Scope.COMMIT), C.entry(Scope.COMMIT)),
        )
        genesis = keep(store, Snapshot(trust_root=first))
        endorse(store, genesis, A)
        main = advance(store, genesis, "mainline work")
        endorse(store, main, A)
        side = advance(store, genesis, "stolen key era")
        endorse(store, side, C)
        merge = keep(store, main.reconciled(main.modules.values(), [side.digest]))
        endorse(store, merge, C)
        recorded = TrustRoot(
            revision=2,
            govern_quorum=1,
            keys=(
                A.entry(Scope.GOVERN, Scope.COMMIT),
                C.entry(Scope.COMMIT, compromised_from=side.digest),
            ),
        )
        head = keep(store, merge.with_trust_root(recorded))
        endorse(store, head, A)
        return side, merge, head

    def test_a_compromise_behind_the_second_parent_withdraws(self, store: MemoryBlockStore) -> None:
        from boltzmann.authenticity.chain import descends_from

        side, merge, head = self.build(store)
        assert descends_from(store, merge, side.digest) is True
        report = Authenticator(store).authenticate(merge, current=head.trust_root)
        assert report.state is AuthorshipState.UNAUTHORIZED
        assert [verdict.key for verdict in report.withdrawn] == [C.public_key.fingerprint]
        with pytest.raises(CompromisedKeyError):
            report.require_authorized()

    def test_a_fully_resolved_merge_clears_what_it_does_not_contain(self, store: MemoryBlockStore) -> None:
        # Precision, not blanket refusal: every branch closed at the genesis, so a position
        # nowhere in the DAG is a definitive False and honest governors stay admitted.
        from boltzmann.authenticity.chain import descends_from

        _, merge, _ = self.build(store)
        assert descends_from(store, merge, OciDigest.of(b"nowhere in this history")) is False

    def test_an_unresolvable_merged_parent_is_undecidable_not_cleared(self, store: MemoryBlockStore) -> None:
        from boltzmann.authenticity.chain import descends_from

        first = TrustRoot(
            revision=1,
            govern_quorum=1,
            keys=(A.entry(Scope.GOVERN, Scope.COMMIT), C.entry(Scope.COMMIT)),
        )
        genesis = keep(store, Snapshot(trust_root=first))
        main = advance(store, genesis, "mainline work")
        withheld = OciDigest.of(b"a side history nobody shipped")
        merge = keep(store, main.reconciled(main.modules.values(), [withheld]))
        endorse(store, merge, C)

        behind = OciDigest.of(b"the era hidden behind the withheld parent")
        assert descends_from(store, merge, behind) is None
        # The withheld position itself is still named by the merge, so it is decidedly reached.
        assert descends_from(store, merge, withheld) is True

        recorded = TrustRoot(
            revision=2,
            govern_quorum=1,
            keys=(
                A.entry(Scope.GOVERN, Scope.COMMIT),
                C.entry(Scope.COMMIT, compromised_from=behind),
            ),
        )
        head = keep(store, merge.with_trust_root(recorded))
        report = Authenticator(store).authenticate(merge, current=head.trust_root)
        assert report.state is AuthorshipState.UNAUTHORIZED
        assert SignatureOutcome.COMPROMISE_UNDECIDABLE in report.outcomes().values(), (
            "an undecidable compromise is not a cleared one"
        )


class TestDeepTruncation:
    """A walk that ends at an unresolvable non-genesis position is the self-admission attack
    one step removed: whoever withheld the parent also withheld the proof that the oldest
    reachable authority was ever granted."""

    def fabricate(self, store: MemoryBlockStore) -> Snapshot:
        """P claims trust_root=TR_evil and a first parent X that is deliberately never shipped;
        the head is an ordinary commit on top, signed by the evil key."""
        evil = TrustRoot(
            revision=7,
            govern_quorum=1,
            keys=(C.entry(Scope.GOVERN, Scope.COMMIT, since=7),),
        )
        withheld = OciDigest.of(b"the history that would show the rotation was never approved")
        fabricated = keep(store, Snapshot(trust_root=evil, parents=[withheld]))
        head = advance(store, fabricated, "loot")
        endorse(store, head, C)
        return head

    def test_a_withheld_ancestor_blocks_instead_of_authorizing(self, store: MemoryBlockStore) -> None:
        head = self.fabricate(store)
        report = Authenticator(store).authenticate(head)
        # The signature itself is flawless -- what fails is the chain that carries it.
        assert report.outcomes()[C.public_key.fingerprint] is SignatureOutcome.VALID
        assert report.has(FindingKind.CHAIN_TRUNCATED)
        assert report.state is AuthorshipState.UNAUTHORIZED
        with pytest.raises(UnauthorizedKeyError):
            report.require_authorized()

    def test_a_pin_matched_above_the_gap_anchors_it(self, store: MemoryBlockStore) -> None:
        from boltzmann.authenticity.pins import PinSource, write_pin

        head = self.fabricate(store)
        write_pin(store, head.trust_root.digest, PinSource.OUT_OF_BAND)  # type: ignore[union-attr]
        report = Authenticator(store).authenticate(head)
        truncations = [finding for finding in report.findings if finding.kind is FindingKind.CHAIN_TRUNCATED]
        assert truncations, "the gap is still reported"
        assert all(not finding.blocking for finding in truncations)
        assert report.state is AuthorshipState.AUTHORIZED

    def test_a_complete_chain_reports_no_truncation(self, store: MemoryBlockStore, chain: Snapshot) -> None:
        report = Authenticator(store).authenticate(chain)
        assert not report.has(FindingKind.CHAIN_TRUNCATED)
        assert report.state is AuthorshipState.AUTHORIZED


class TestRevisionDiscipline:
    """A revision changes the key list and nothing else."""

    def test_a_revision_that_also_changed_content_is_rejected(self, store: MemoryBlockStore, chain: Snapshot) -> None:
        revised = TrustRoot(revision=2, govern_quorum=2, keys=revision_one().keys)
        smuggled = advance(store, chain, "concept one", "smuggled")
        folded = keep(
            store,
            Snapshot(
                modules=smuggled.modules,
                parents=[chain.digest],
                trust_root=revised,
            ),
        )
        endorse(store, folded, A, B)
        report = Authenticator(store).authenticate(folded)
        assert report.has(FindingKind.REVISION_CHANGED_CONTENT)
        assert report.state is AuthorshipState.UNAUTHORIZED

    def test_a_since_claim_earlier_than_the_chain_supports_is_refuted(
        self, store: MemoryBlockStore, chain: Snapshot
    ) -> None:
        liar = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(*revision_one().keys, C.entry(Scope.COMMIT, since=1)),
        )
        eight = keep(store, chain.with_trust_root(liar))
        endorse(store, eight, A, B)
        nine = advance(store, eight, "concept one", "more")
        endorse(store, nine, C)
        report = Authenticator(store).authenticate(nine)
        assert report.has(FindingKind.SINCE_REFUTED)
        assert report.state is AuthorshipState.UNAUTHORIZED


class TestPolicy:
    """The policy decides tolerances, never reporting."""

    def test_a_two_signature_policy_requires_two_distinct_keys(self, store: MemoryBlockStore, chain: Snapshot) -> None:
        strict = VerificationPolicy(required_signatures=2)
        report = Authenticator(store, policy=strict).authenticate(chain)
        assert report.state is AuthorshipState.UNAUTHORIZED
        assert report.has(FindingKind.SIGNATURES_BELOW_POLICY)

    def test_a_proposal_head_is_refused_by_default_and_admitted_by_policy(
        self, store: MemoryBlockStore, chain: Snapshot
    ) -> None:
        revised = TrustRoot(
            revision=2,
            govern_quorum=2,
            keys=(*revision_one().keys, C.entry(Scope.PROPOSE, since=2)),
        )
        eight = keep(store, chain.with_trust_root(revised))
        endorse(store, eight, A, B)
        proposal = advance(store, eight, "concept one", "contribution")
        endorse(store, proposal, C)

        default = Authenticator(store).authenticate(proposal)
        assert default.outcomes()[C.public_key.fingerprint] is SignatureOutcome.VALID_AS_PROPOSAL
        assert default.has(FindingKind.PROPOSED_HEAD)
        assert default.state is AuthorshipState.UNAUTHORIZED
        assert default.is_proposal

        permissive = VerificationPolicy(allow_propose_head=True)
        allowed = Authenticator(store, policy=permissive).authenticate(proposal)
        assert allowed.state is AuthorshipState.AUTHORIZED
        assert allowed.is_proposal
