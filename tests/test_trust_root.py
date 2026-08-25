"""The trust root: a key list whose digest is an identity and whose claims are checkable.

Two properties carry the weight. Canonical form -- one spelling per key, no duplicate scopes, no
comment -- because two spellings of one list would be two digests and a pin has to mean
something. And ``since``-confirmability by refutation, because a history may be legitimately
truncated: presence in an unobserved revision cannot be proven, absence from an observed one can.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from boltzmann.authenticity import (
    Scope,
    SinceVerdict,
    SshPublicKey,
    TrustedKey,
    TrustRoot,
    confirm_since,
    put_string,
)
from boltzmann.constants import SNAPSHOT_NAMESPACE
from boltzmann.identity.digest import OciDigest


def key(seed: bytes) -> SshPublicKey:
    """A syntactically valid Ed25519 key from 32 deterministic bytes. Never a real key."""
    point = (seed * 32)[:32]
    return SshPublicKey.from_blob(put_string(b"ssh-ed25519") + put_string(point))


KEY_A = key(b"\x0a")
KEY_B = key(b"\x0b")
KEY_C = key(b"\x0c")


def entry(public: SshPublicKey, *scopes: Scope, since: int = 1, **positions: object) -> TrustedKey:
    return TrustedKey(key=public, scopes=tuple(scopes) or (Scope.COMMIT,), since=since, **positions)


def root(*entries: TrustedKey, revision: int = 1, quorum: int = 1) -> TrustRoot:
    return TrustRoot(revision=revision, govern_quorum=quorum, keys=entries)


class TestTrustedKey:
    """An entry's positions must cohere, and its scopes are a set."""

    def test_a_duplicate_scope_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="lists one twice"):
            TrustedKey(key=KEY_A, scopes=(Scope.COMMIT, Scope.COMMIT), since=1)

    def test_a_key_retired_at_or_before_its_admission_was_never_authorized(self) -> None:
        with pytest.raises(ValidationError, match="never authorized"):
            entry(KEY_A, Scope.COMMIT, since=2, retired_from=2)

    def test_an_empty_scope_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TrustedKey(key=KEY_A, scopes=(), since=1)

    def test_a_key_with_a_comment_is_rejected_at_the_key_layer(self) -> None:
        with pytest.raises(Exception, match=r"canonical|two identities|exactly"):
            TrustedKey(key=KEY_A.authorized_key + " alice@example", scopes=(Scope.COMMIT,), since=1)  # type: ignore[arg-type]

    def test_holds_reads_scopes_and_nothing_positional(self) -> None:
        retired = entry(KEY_A, Scope.GOVERN, since=1, retired_from=2)
        assert retired.holds(Scope.GOVERN)
        assert not retired.holds(Scope.COMMIT)


class TestTrustRoot:
    """Documents no conforming init or rotate could produce are refused at construction."""

    def test_a_key_listed_twice_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="more than once"):
            root(entry(KEY_A, Scope.GOVERN), entry(KEY_A, Scope.COMMIT))

    def test_a_root_with_no_govern_holder_could_never_be_revised(self) -> None:
        with pytest.raises(ValidationError, match="never be revised"):
            root(entry(KEY_A, Scope.COMMIT))

    def test_a_retired_govern_holder_still_satisfies_construction(self) -> None:
        # Retirement can legitimately leave a quorum unreachable; that is the verifier's warning,
        # not a construction failure -- the *entry* exists, which is what the validator checks.
        document = root(entry(KEY_A, Scope.GOVERN, since=1, retired_from=2), revision=2)
        assert document.govern_holders == ()

    def test_an_admission_after_the_document_revision_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="after this"):
            root(entry(KEY_A, Scope.GOVERN, since=3), revision=2)

    def test_a_retirement_the_document_cannot_yet_know_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot yet know"):
            root(entry(KEY_A, Scope.GOVERN, since=1, retired_from=5), revision=2)

    def test_a_later_protocol_version_is_refused_rather_than_read(self) -> None:
        with pytest.raises(ValidationError):
            TrustRoot(boltzmann=2, revision=1, govern_quorum=1, keys=(entry(KEY_A, Scope.GOVERN),))

    def test_a_foreign_namespace_is_not_for_this_brain(self) -> None:
        with pytest.raises(ValidationError, match="another namespace"):
            TrustRoot(
                revision=1,
                namespace="git",
                govern_quorum=1,
                keys=(entry(KEY_A, Scope.GOVERN),),
            )

    def test_an_empty_key_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TrustRoot(revision=1, govern_quorum=1, keys=())


class TestIdentity:
    """The digest is over canonical bytes, and the wire form is the paper's."""

    def test_the_digest_is_deterministic(self) -> None:
        one = root(entry(KEY_A, Scope.GOVERN, Scope.COMMIT))
        two = root(entry(KEY_A, Scope.GOVERN, Scope.COMMIT))
        assert one.digest == two.digest

    def test_scope_order_is_part_of_the_identity(self) -> None:
        # The document travels as bytes and is never reconstructed, so authored order is kept --
        # and therefore committed to.
        one = root(entry(KEY_A, Scope.GOVERN, Scope.COMMIT))
        two = root(entry(KEY_A, Scope.COMMIT, Scope.GOVERN))
        assert one.digest != two.digest

    def test_the_wire_form_carries_the_papers_field_names_and_no_nulls(self) -> None:
        document = json.loads(root(entry(KEY_A, Scope.GOVERN)).canonical_bytes())
        assert set(document) == {"boltzmann", "revision", "namespace", "govern_quorum", "keys"}
        assert document["namespace"] == SNAPSHOT_NAMESPACE
        assert set(document["keys"][0]) == {"key", "scopes", "since"}
        assert document["keys"][0]["key"] == KEY_A.authorized_key

    def test_a_compromise_position_is_a_snapshot_digest_on_the_wire(self) -> None:
        position = OciDigest.of(b"snapshot S41")
        document = root(
            entry(KEY_A, Scope.GOVERN),
            entry(KEY_B, Scope.COMMIT, compromised_from=position),
        )
        raw = json.loads(document.canonical_bytes())
        assert raw["keys"][1]["compromised_from"] == str(position)

    def test_entry_lookup_matches_on_the_blob_not_the_fingerprint(self) -> None:
        document = root(entry(KEY_A, Scope.GOVERN), entry(KEY_B, Scope.COMMIT))
        found = document.entry_for(SshPublicKey.from_blob(KEY_B.blob))
        assert found is not None
        assert found.key.matches(KEY_B)
        assert document.entry_for(KEY_C) is None


class TestPositionalQueries:
    """Retirement takes effect at a revision and never before it."""

    def test_retirement_is_recorded_by_the_revision_it_takes_effect_at(self) -> None:
        # The revision-2 document carries no marker -- the retirement is added BY revision 3,
        # which is exactly what makes it non-retroactive: the trust root in force at earlier
        # positions is the earlier document, and it says nothing about a departure it predates.
        unmarked = entry(KEY_A, Scope.GOVERN, since=1)
        marked = entry(KEY_A, Scope.GOVERN, since=1, retired_from=3)
        before = root(unmarked, entry(KEY_B, Scope.GOVERN), revision=2)
        at = root(marked, entry(KEY_B, Scope.GOVERN), revision=3)
        assert not before.is_retired(unmarked)
        assert at.is_retired(marked)

    def test_holders_excludes_the_retired(self) -> None:
        document = root(
            entry(KEY_A, Scope.GOVERN, since=1, retired_from=2),
            entry(KEY_B, Scope.GOVERN),
            revision=2,
        )
        assert [holder.fingerprint for holder in document.govern_holders] == [KEY_B.fingerprint]


class TestConfirmSince:
    """Refutation, not proof: absence from an observed revision is what convicts."""

    def test_a_claim_whose_admitting_revision_was_observed_is_confirmed(self) -> None:
        admitted = entry(KEY_B, Scope.COMMIT, since=2)
        revisions = [
            root(entry(KEY_A, Scope.GOVERN), revision=1),
            root(entry(KEY_A, Scope.GOVERN), admitted, revision=2, quorum=1),
        ]
        assert confirm_since(revisions, admitted) is SinceVerdict.CONFIRMED

    def test_a_key_absent_from_an_observed_revision_after_its_claim_is_refuted(self) -> None:
        # The lie this rule exists for: a key that just added itself at revision 3 claiming to
        # have been authorized since revision 1.
        liar = entry(KEY_C, Scope.COMMIT, since=1)
        revisions = [
            root(entry(KEY_A, Scope.GOVERN), revision=1),
            root(entry(KEY_A, Scope.GOVERN), revision=2),
            root(entry(KEY_A, Scope.GOVERN), liar, revision=3),
        ]
        assert confirm_since(revisions, liar) is SinceVerdict.REFUTED

    def test_a_truncated_chain_is_unobservable_not_confirmed(self) -> None:
        old = entry(KEY_B, Scope.COMMIT, since=1)
        revisions = [root(entry(KEY_A, Scope.GOVERN), old, revision=3)]
        assert confirm_since(revisions, old) is SinceVerdict.UNOBSERVABLE

    def test_a_key_present_everywhere_since_admission_is_never_refuted(self) -> None:
        steady = entry(KEY_B, Scope.COMMIT, since=1)
        revisions = [root(entry(KEY_A, Scope.GOVERN), steady, revision=index, quorum=1) for index in (1, 2, 3)]
        assert confirm_since(revisions, steady) is SinceVerdict.CONFIRMED
